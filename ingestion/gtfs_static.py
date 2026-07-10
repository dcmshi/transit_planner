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

import asyncio
import io
import logging
import zipfile
from datetime import datetime

import httpx
import pandas as pd
from sqlalchemy.orm import Session

from config import DATA_DIR, DATABASE_URL, GTFS_STATIC_URL
from db.models import (
    Route,
    ServiceCalendar,
    ServiceCalendarDate,
    Stop,
    StopTime,
    Trip,
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

        # Clear child tables first: stop_times references stops and trips,
        # trips references routes.  The parsers below delete their own table
        # before inserting, but they run parents-first, which violates FK
        # constraints on PostgreSQL when data already exists (re-ingest).
        session.query(StopTime).delete()
        session.query(Trip).delete()
        session.flush()

        _parse_stops(read("stops.txt"), session)
        _parse_routes(read("routes.txt"), session)
        _parse_trips(read("trips.txt"), session)
        _parse_stop_times(read("stop_times.txt"), session)

        if "calendar.txt" in names:
            _parse_calendar(read("calendar.txt"), session)
        if "calendar_dates.txt" in names:
            _parse_calendar_dates(read("calendar_dates.txt"), session)

    _validate_service_id_convention(session)

    session.commit()
    logger.info("GTFS static data committed to database.")


def _validate_service_id_convention(session: Session) -> None:
    """
    Routing (_find_trip_legs) selects trips with service_id = <travel date>,
    relying on the GO feed convention that service_id values are YYYYMMDD
    dates.  A feed that switched to standard weekly service_ids would make
    every route query silently return nothing — abort the ingest instead
    (the transaction is not yet committed, so the previous data survives).
    Isolated non-date values only get a warning: their trips are unroutable
    but the rest of the feed still works.
    """
    session.flush()
    service_ids = {row[0] for row in session.query(Trip.service_id).distinct()}
    non_date: set[str] = set()
    for sid in service_ids:
        try:
            datetime.strptime(sid, "%Y%m%d")
        except (TypeError, ValueError):
            non_date.add(sid)

    if service_ids and non_date == service_ids:
        raise ValueError(
            "GTFS feed convention change: no trip service_id parses as a "
            f"YYYYMMDD date (samples: {sorted(non_date)[:5]}). Routing "
            "filters trips by service_id = travel date and would return no "
            "results — aborting ingest."
        )
    if non_date:
        logger.warning(
            "%d of %d service_id values are not YYYYMMDD dates (samples: %s). "
            "Trips on these services will never be selected by routing.",
            len(non_date), len(service_ids), sorted(non_date)[:5],
        )


def _parse_stops(df: pd.DataFrame, session: Session) -> None:
    session.query(Stop).delete()
    stops = []
    for row in df.to_dict("records"):
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
        stops.append(stop)
    session.bulk_save_objects(stops)
    logger.info("Loaded %d stops.", len(df))


def _parse_routes(df: pd.DataFrame, session: Session) -> None:
    session.query(Route).delete()
    routes = [
        Route(
            route_id=row["route_id"],
            route_short_name=row.get("route_short_name", ""),
            route_long_name=row.get("route_long_name", ""),
            route_type=int(row["route_type"]) if row.get("route_type") else 3,
        )
        for row in df.to_dict("records")
    ]
    session.bulk_save_objects(routes)
    logger.info("Loaded %d routes.", len(df))


def _parse_trips(df: pd.DataFrame, session: Session) -> None:
    session.query(Trip).delete()
    session.flush()  # ensure route rows from _parse_routes are visible
    valid_routes = {r[0] for r in session.query(Route.route_id).all()}
    trips = []
    skipped = 0
    for row in df.to_dict("records"):
        if row["route_id"] not in valid_routes:
            skipped += 1
            continue
        trips.append(Trip(
            trip_id=row["trip_id"],
            route_id=row["route_id"],
            service_id=row["service_id"],
            trip_headsign=row.get("trip_headsign", ""),
            direction_id=int(row["direction_id"]) if row.get("direction_id") else 0,
            shape_id=row.get("shape_id", ""),
        ))
    if skipped:
        logger.warning("Skipped %d trips with invalid route_id.", skipped)
    session.bulk_save_objects(trips)
    logger.info("Loaded %d trips.", len(trips))


def _parse_stop_times(df: pd.DataFrame, session: Session) -> None:
    session.query(StopTime).delete()
    session.flush()  # ensure trip/stop rows from prior parsers are visible
    # Filter to only valid (trip_id, stop_id) pairs — the GTFS feed occasionally
    # contains stop_times that reference trips or stops not present in the feed.
    # SQLite silently ignores FK violations; PostgreSQL raises immediately.
    valid_trips = {r[0] for r in session.query(Trip.trip_id).all()}
    valid_stops = {r[0] for r in session.query(Stop.stop_id).all()}
    # stop_times is by far the largest feed file (~2M rows for GO Transit) —
    # iterate tuples and save in chunks rather than materialising 2M dicts
    # plus 2M ORM objects at once (multi-GB peak).
    chunk_size = 50_000
    batch: list[StopTime] = []
    loaded = 0
    skipped = 0
    for row in df.itertuples(index=False):
        if row.trip_id not in valid_trips or row.stop_id not in valid_stops:
            skipped += 1
            continue
        batch.append(StopTime(
            trip_id=row.trip_id,
            arrival_time=row.arrival_time,
            departure_time=row.departure_time,
            stop_id=row.stop_id,
            stop_sequence=int(row.stop_sequence),
        ))
        if len(batch) >= chunk_size:
            session.bulk_save_objects(batch)
            loaded += len(batch)
            batch = []
    if batch:
        session.bulk_save_objects(batch)
        loaded += len(batch)
    if skipped:
        logger.warning("Skipped %d stop_times with invalid trip_id or stop_id.", skipped)
    logger.info("Loaded %d stop times.", loaded)


def _parse_calendar(df: pd.DataFrame, session: Session) -> None:
    session.query(ServiceCalendar).delete()
    session.bulk_save_objects([
        ServiceCalendar(
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
        )
        for row in df.to_dict("records")
    ])
    logger.info("Loaded %d calendar entries.", len(df))


def _parse_calendar_dates(df: pd.DataFrame, session: Session) -> None:
    session.query(ServiceCalendarDate).delete()
    session.bulk_save_objects([
        ServiceCalendarDate(
            service_id=row["service_id"],
            date=row["date"],
            exception_type=int(row["exception_type"]),
        )
        for row in df.to_dict("records")
    ])
    logger.info("Loaded %d calendar date exceptions.", len(df))


async def refresh_static_data(session: Session) -> None:
    """Download and ingest a fresh copy of GTFS static data."""
    zip_bytes = await download_gtfs_zip()
    # parse_and_store is the heaviest stage of the whole refresh (pandas
    # parse + ~2M-row insert) — run it in a worker thread so the event loop
    # keeps serving /health, /ingest/status, and RT polls meanwhile.
    await asyncio.to_thread(parse_and_store, zip_bytes, session)
