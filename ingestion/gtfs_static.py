"""
Downloads and parses the GO Transit GTFS static feed into the local database.

Feed contents used:
  stops.txt          → Stop
  routes.txt         → Route
  trips.txt          → Trip
  stop_times.txt     → StopTime (and the derived StopRoute lookup)
  shapes.txt         → Shape (+ derived ShapeStopPosition projections)
  calendar.txt       → ServiceCalendar
  calendar_dates.txt → ServiceCalendarDate
"""

import asyncio
import bisect
import io
import json
import logging
import math
import re
import zipfile
from collections import defaultdict
from datetime import datetime
from typing import Any, cast

import httpx
import pandas as pd
from shapely.geometry import LineString, Point
from sqlalchemy import CursorResult
from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from config import DATA_DIR, DATABASE_URL, GTFS_STATIC_URL
from db.models import (
    Route,
    ServiceCalendar,
    ServiceCalendarDate,
    Shape,
    ShapeStopPosition,
    Stop,
    StopRoute,
    StopTime,
    Trip,
)

# shapely (imported above) is a hard dependency and shape projection needs it
# on every backend; only the geoalchemy2 bridge is optional.
try:
    from geoalchemy2.shape import from_shape
    _HAS_POSTGIS = DATABASE_URL.startswith("postgresql")
except ImportError:
    _HAS_POSTGIS = False

logger = logging.getLogger(__name__)

GTFS_ZIP_PATH = DATA_DIR / "gtfs_static.zip"


# Hours may exceed 23 for post-midnight trips but must stay two digits: the
# routing query and the no-show sweep both read the hour with
# substr(departure_time, 1, 2), so a three-digit hour would slice wrong just
# as a one-digit hour does.
_GTFS_TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")


def _normalise_gtfs_time(value: str) -> str | None:
    """Zero-pad a GTFS time to HH:MM:SS, or None if it cannot be trusted.

    The spec accepts H:MM:SS as well as HH:MM:SS, but every SQL site slices
    fixed character offsets out of these strings.  Left unpadded, "9:30:00"
    makes PostgreSQL raise on CAST('9:' AS INT) and makes SQLite silently
    return 09:00:00 — a half-hour error with no warning.  Pad once here, at
    the boundary, instead of teaching each query to cope.
    """
    match = _GTFS_TIME_RE.match(value.strip())
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return f"{int(hours):02d}:{minutes}:{seconds}"


def _int_or(value: Any, default: int) -> int:
    """int(value), or default for blank/garbage — one bad optional field
    must not abort a 2M-row ingest."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


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
        session.query(ShapeStopPosition).delete()
        session.query(StopRoute).delete()
        session.query(StopTime).delete()
        session.query(Trip).delete()
        # Calendar tables are cleared unconditionally: their parsers only
        # run when the file is present in the zip, so a feed that drops
        # calendar_dates.txt would otherwise leave stale exception_type=2
        # rows suppressing trips forever.
        session.query(ServiceCalendar).delete()
        session.query(ServiceCalendarDate).delete()
        session.flush()

        _parse_stops(read("stops.txt"), session)
        _parse_routes(read("routes.txt"), session)
        _parse_trips(read("trips.txt"), session)
        _parse_stop_times(read("stop_times.txt"), session)
        _build_stop_routes(session)

        # Track geometry is optional: a feed without shapes.txt still routes,
        # its legs simply carry no polyline.
        if "shapes.txt" in names:
            _parse_shapes(read("shapes.txt"), session)
            _build_shape_stop_positions(session)
        else:
            session.query(Shape).delete()
            logger.info("Feed has no shapes.txt; legs will carry no geometry.")

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
    # Real GTFS zips occasionally repeat primary keys — a duplicate would
    # abort the whole ingest with an IntegrityError at commit.
    df = df.drop_duplicates(subset="stop_id")
    stops = []
    skipped = 0
    for row in df.to_dict("records"):
        try:
            lat = float(row["stop_lat"])
            lon = float(row["stop_lon"])
        except (ValueError, TypeError):
            skipped += 1  # blank/garbage coordinates — skip, don't abort
            continue
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
    if skipped:
        logger.warning("Skipped %d stops with unparseable coordinates.", skipped)
    session.bulk_save_objects(stops)
    logger.info("Loaded %d stops.", len(stops))


def _parse_routes(df: pd.DataFrame, session: Session) -> None:
    session.query(Route).delete()
    df = df.drop_duplicates(subset="route_id")
    routes = [
        Route(
            route_id=row["route_id"],
            route_short_name=row.get("route_short_name", ""),
            route_long_name=row.get("route_long_name", ""),
            route_type=_int_or(row.get("route_type"), 3),  # 3 = bus
        )
        for row in df.to_dict("records")
    ]
    session.bulk_save_objects(routes)
    logger.info("Loaded %d routes.", len(df))


def _parse_trips(df: pd.DataFrame, session: Session) -> None:
    session.query(Trip).delete()
    session.flush()  # ensure route rows from _parse_routes are visible
    df = df.drop_duplicates(subset="trip_id")
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
            direction_id=_int_or(row.get("direction_id"), 0),
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
    blank_times = 0
    malformed_times = 0
    for row in df.itertuples(index=False):
        if row.trip_id not in valid_trips or row.stop_id not in valid_stops:
            skipped += 1
            continue
        # Non-timepoint rows may legally omit times; "" would corrupt graph
        # weights (hms_to_seconds("") == 0) and crash PostgreSQL's CAST in
        # the routing query — skip them.
        if not row.arrival_time or not row.departure_time:
            blank_times += 1
            continue
        arrival_time = _normalise_gtfs_time(str(row.arrival_time))
        departure_time = _normalise_gtfs_time(str(row.departure_time))
        if arrival_time is None or departure_time is None:
            malformed_times += 1
            continue
        try:
            # Ordering-critical — a garbage value can't be defaulted, but
            # one bad row must not abort the whole ingest either.
            stop_sequence = int(cast(Any, row.stop_sequence))
        except (ValueError, TypeError):
            skipped += 1
            continue
        batch.append(StopTime(
            trip_id=row.trip_id,
            arrival_time=arrival_time,
            departure_time=departure_time,
            stop_id=row.stop_id,
            stop_sequence=stop_sequence,
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
    if blank_times:
        logger.warning("Skipped %d stop_times with blank arrival/departure times.", blank_times)
    if malformed_times:
        logger.warning(
            "Skipped %d stop_times whose times are not H:MM:SS or HH:MM:SS.", malformed_times
        )
    logger.info("Loaded %d stop times.", loaded)


def _parse_shapes(df: pd.DataFrame, session: Session) -> None:
    """Store each shape as one ordered polyline row."""
    session.query(Shape).delete()
    session.flush()

    df = df.copy()
    for column in ("shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    # The feed does not guarantee sequence order — GO ships some shapes
    # descending — and a mis-ordered polyline would draw as a scribble.
    df = df.dropna(subset=["shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"])
    df = df.sort_values(["shape_id", "shape_pt_sequence"])

    shapes = []
    for shape_id, group in df.groupby("shape_id", sort=False):
        points = [
            [round(lon, 6), round(lat, 6)]
            for lon, lat in zip(group["shape_pt_lon"], group["shape_pt_lat"])
        ]
        if len(points) < 2:
            continue  # a single point is not a line
        shapes.append(Shape(shape_id=str(shape_id), points=json.dumps(points)))
    session.bulk_save_objects(shapes)
    logger.info("Loaded %d shapes (%d points).", len(shapes), len(df))


def _build_shape_stop_positions(session: Session) -> None:
    """
    Project every stop onto the shapes whose trips serve it.

    Neither shapes.txt nor stop_times.txt carries shape_dist_traveled in the
    GO feed, so the position of a stop along its shape has to be derived.
    Without it a leg cannot be cut out of the trip's polyline.
    """
    session.query(ShapeStopPosition).delete()
    session.flush()

    shapes = {s.shape_id: json.loads(s.points) for s in session.query(Shape).all()}
    if not shapes:
        logger.info("No shapes ingested; skipping stop projection.")
        return

    stop_xy = {
        stop_id: (lon, lat)
        for stop_id, lat, lon in session.query(Stop.stop_id, Stop.stop_lat, Stop.stop_lon)
    }
    # Which stops each shape actually serves, via the trips that use it.
    pairs = session.execute(
        sa_select(Trip.shape_id, StopTime.stop_id)
        .join(StopTime, StopTime.trip_id == Trip.trip_id)
        .where(Trip.shape_id.isnot(None), Trip.shape_id != "")
        .distinct()
    ).all()

    served_by_shape: dict[str, set[str]] = defaultdict(set)
    for shape_id, stop_id in pairs:
        served_by_shape[shape_id].add(stop_id)

    positions: list[ShapeStopPosition] = []
    unmatched = 0
    for shape_id, points in shapes.items():
        served = served_by_shape.get(shape_id)
        if not served:
            continue
        line = LineString(points)
        vertex_distances = _cumulative_distances(points)
        for stop_id in served:
            xy = stop_xy.get(stop_id)
            if xy is None:
                unmatched += 1
                continue
            positions.append(ShapeStopPosition(
                shape_id=shape_id,
                stop_id=stop_id,
                point_index=_nearest_vertex(vertex_distances, line.project(Point(*xy))),
            ))

    session.bulk_save_objects(positions)
    if unmatched:
        logger.warning("%d shape/stop pairs had no stop coordinates.", unmatched)
    logger.info("Projected %d stop positions onto %d shapes.", len(positions), len(shapes))


def _cumulative_distances(points: list[list[float]]) -> list[float]:
    """Planar distance to each vertex, matching LineString.project()'s units."""
    distances = [0.0]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        distances.append(distances[-1] + math.hypot(x1 - x0, y1 - y0))
    return distances


def _nearest_vertex(vertex_distances: list[float], distance: float) -> int:
    """Index of the vertex nearest `distance` along the line.

    Binary search rather than scanning: shapes run to 4,600 points and every
    stop on every shape is projected, so a linear scan here would dominate
    the ingest.  An index rather than the raw distance keeps request-time
    slicing to a plain list slice.
    """
    i = bisect.bisect_left(vertex_distances, distance)
    if i == 0:
        return 0
    if i >= len(vertex_distances):
        return len(vertex_distances) - 1
    before, after = vertex_distances[i - 1], vertex_distances[i]
    return i - 1 if (distance - before) <= (after - distance) else i


def _build_stop_routes(session: Session) -> None:
    """
    Materialise the stop → routes mapping that /stops used to derive per
    request from a DISTINCT over the stop_times/trips join.

    Computed in SQL rather than pulled into Python: the source is the largest
    table in the database and the result is a few thousand rows.
    """
    session.query(StopRoute).delete()
    session.flush()
    # Session.execute() is typed Result[Any]; DML always yields a CursorResult,
    # which is where rowcount lives.
    result = cast(CursorResult[Any], session.execute(
        sa_insert(StopRoute).from_select(
            ["stop_id", "route_id"],
            sa_select(StopTime.stop_id, Trip.route_id)
            .join(Trip, Trip.trip_id == StopTime.trip_id)
            .where(StopTime.stop_id.isnot(None), Trip.route_id.isnot(None))
            .distinct(),
        )
    ))
    logger.info("Built %d stop/route pairs.", result.rowcount)


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
    exceptions = []
    skipped = 0
    for row in df.to_dict("records"):
        try:
            exception_type = int(row["exception_type"])
        except (ValueError, TypeError):
            skipped += 1  # blank/garbage exception_type — skip, don't abort
            continue
        exceptions.append(ServiceCalendarDate(
            service_id=row["service_id"],
            date=row["date"],
            exception_type=exception_type,
        ))
    if skipped:
        logger.warning("Skipped %d calendar_dates rows with bad exception_type.", skipped)
    session.bulk_save_objects(exceptions)
    logger.info("Loaded %d calendar date exceptions.", len(exceptions))


async def refresh_static_data(session: Session) -> None:
    """Download and ingest a fresh copy of GTFS static data."""
    zip_bytes = await download_gtfs_zip()
    # parse_and_store is the heaviest stage of the whole refresh (pandas
    # parse + ~2M-row insert) — run it in a worker thread so the event loop
    # keeps serving /health, /ingest/status, and RT polls meanwhile.
    await asyncio.to_thread(parse_and_store, zip_bytes, session)
