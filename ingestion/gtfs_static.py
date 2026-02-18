"""
Downloads and parses the GO Transit GTFS static feed into the local database.

Feed contents used:
  stops.txt          → Stop
  routes.txt         → Route
  trips.txt          → Trip
  stop_times.txt     → StopTime
  calendar.txt       → ServiceCalendar
  calendar_dates.txt → ServiceCalendarDate
"""

import io
import logging
import zipfile
from pathlib import Path

import httpx
import pandas as pd
from sqlalchemy.orm import Session

from config import DATA_DIR, DATABASE_URL, GTFS_STATIC_URL
from db.models import (
    Route, ServiceCalendar, ServiceCalendarDate, Stop, StopTime, Trip,
)

try:
    from geoalchemy2.shape import from_shape
    from shapely.geometry import Point
    _HAS_POSTGIS = DATABASE_URL.startswith("postgresql")
except ImportError:
    _HAS_POSTGIS = False

logger = logging.getLogger(__name__)

GTFS_ZIP_PATH = DATA_DIR / "gtfs_static.zip"


async def download_gtfs_zip(url: str = GTFS_STATIC_URL) -> bytes:
    """Download GTFS zip from the given URL and cache it to disk."""
    if not url:
        raise ValueError("GTFS_STATIC_URL is not configured. Set it in your .env file.")
    logger.info("Downloading GTFS static feed from %s", url)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
    GTFS_ZIP_PATH.write_bytes(response.content)
    logger.info("Saved GTFS zip to %s (%d bytes)", GTFS_ZIP_PATH, len(response.content))
    return response.content


def parse_and_store(zip_bytes: bytes, session: Session) -> None:
    """
    Extract GTFS zip and upsert all relevant feed data into the database.
    Clears existing data before inserting fresh records.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        logger.info("GTFS zip contains: %s", names)

        def read(filename: str) -> pd.DataFrame:
            with zf.open(filename) as f:
                return pd.read_csv(f, dtype=str).fillna("")

        _parse_stops(read("stops.txt"), session)
        _parse_routes(read("routes.txt"), session)
        _parse_trips(read("trips.txt"), session)
        _parse_stop_times(read("stop_times.txt"), session)

        if "calendar.txt" in names:
            _parse_calendar(read("calendar.txt"), session)
        if "calendar_dates.txt" in names:
            _parse_calendar_dates(read("calendar_dates.txt"), session)

    session.commit()
    logger.info("GTFS static data committed to database.")


def _parse_stops(df: pd.DataFrame, session: Session) -> None:
    session.query(Stop).delete()
    for _, row in df.iterrows():
        lat = float(row["stop_lat"])
        lon = float(row["stop_lon"])
        stop = Stop(
            stop_id=row["stop_id"],
            stop_name=row["stop_name"],
            stop_lat=lat,
            stop_lon=lon,
            stop_code=row.get("stop_code", ""),
        )
        if _HAS_POSTGIS:
            stop.geog = from_shape(Point(lon, lat), srid=4326)
        session.add(stop)
    logger.info("Loaded %d stops.", len(df))


def _parse_routes(df: pd.DataFrame, session: Session) -> None:
    session.query(Route).delete()
    for _, row in df.iterrows():
        session.add(Route(
            route_id=row["route_id"],
            route_short_name=row.get("route_short_name", ""),
            route_long_name=row.get("route_long_name", ""),
            route_type=int(row.get("route_type", 3)),
        ))
    logger.info("Loaded %d routes.", len(df))


def _parse_trips(df: pd.DataFrame, session: Session) -> None:
    session.query(Trip).delete()
    session.flush()  # ensure route rows from _parse_routes are visible
    valid_routes = {r[0] for r in session.query(Route.route_id).all()}
    skipped = 0
    for _, row in df.iterrows():
        if row["route_id"] not in valid_routes:
            skipped += 1
            continue
        session.add(Trip(
            trip_id=row["trip_id"],
            route_id=row["route_id"],
            service_id=row["service_id"],
            trip_headsign=row.get("trip_headsign", ""),
            direction_id=int(row["direction_id"]) if row.get("direction_id") else 0,
            shape_id=row.get("shape_id", ""),
        ))
    if skipped:
        logger.warning("Skipped %d trips with invalid route_id.", skipped)
    logger.info("Loaded %d trips.", len(df) - skipped)


def _parse_stop_times(df: pd.DataFrame, session: Session) -> None:
    session.query(StopTime).delete()
    session.flush()  # ensure trip/stop rows from prior parsers are visible
    # Filter to only valid (trip_id, stop_id) pairs — the GTFS feed occasionally
    # contains stop_times that reference trips or stops not present in the feed.
    # SQLite silently ignores FK violations; PostgreSQL raises immediately.
    valid_trips = {r[0] for r in session.query(Trip.trip_id).all()}
    valid_stops = {r[0] for r in session.query(Stop.stop_id).all()}
    records = []
    skipped = 0
    for _, row in df.iterrows():
        if row["trip_id"] not in valid_trips or row["stop_id"] not in valid_stops:
            skipped += 1
            continue
        records.append(StopTime(
            trip_id=row["trip_id"],
            arrival_time=row["arrival_time"],
            departure_time=row["departure_time"],
            stop_id=row["stop_id"],
            stop_sequence=int(row["stop_sequence"]),
        ))
    if skipped:
        logger.warning("Skipped %d stop_times with invalid trip_id or stop_id.", skipped)
    session.bulk_save_objects(records)
    logger.info("Loaded %d stop times.", len(records))


def _parse_calendar(df: pd.DataFrame, session: Session) -> None:
    session.query(ServiceCalendar).delete()
    for _, row in df.iterrows():
        session.add(ServiceCalendar(
            service_id=row["service_id"],
            monday=row["monday"] == "1",
            tuesday=row["tuesday"] == "1",
            wednesday=row["wednesday"] == "1",
            thursday=row["thursday"] == "1",
            friday=row["friday"] == "1",
            saturday=row["saturday"] == "1",
            sunday=row["sunday"] == "1",
            start_date=row["start_date"],
            end_date=row["end_date"],
        ))
    logger.info("Loaded %d calendar entries.", len(df))


def _parse_calendar_dates(df: pd.DataFrame, session: Session) -> None:
    session.query(ServiceCalendarDate).delete()
    for _, row in df.iterrows():
        session.add(ServiceCalendarDate(
            service_id=row["service_id"],
            date=row["date"],
            exception_type=int(row["exception_type"]),
        ))
    logger.info("Loaded %d calendar date exceptions.", len(df))


async def refresh_static_data(session: Session) -> None:
    """Download and ingest a fresh copy of GTFS static data."""
    zip_bytes = await download_gtfs_zip()
    parse_and_store(zip_bytes, session)
