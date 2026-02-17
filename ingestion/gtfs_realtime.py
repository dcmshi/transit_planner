"""
Polls GO Transit GTFS-Realtime feeds and exposes their latest state.

Feeds polled:
  - Trip Updates   (delays, cancellations, stop-time overrides)
  - Vehicle Positions (optional — locate buses in real time)
  - Service Alerts (human-readable disruption notices)

The parsed state is held in-memory in module-level dicts and updated
every GTFS_RT_POLL_SECONDS seconds by the scheduler started at API boot.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from google.transit import gtfs_realtime_pb2
from sqlalchemy.orm import Session

from config import (
    GTFS_RT_ALERTS_URL,
    GTFS_RT_API_KEY,
    GTFS_RT_POLL_SECONDS,
    GTFS_RT_TRIP_UPDATES_URL,
    GTFS_RT_VEHICLE_POSITIONS_URL,
)
from reliability.historical import record_observed_departure

logger = logging.getLogger(__name__)


@dataclass
class TripUpdateState:
    trip_id: str
    route_id: str
    delay_seconds: int = 0            # positive = late, negative = early
    is_cancelled: bool = False
    stop_time_overrides: dict[str, int] = field(default_factory=dict)  # stop_id → delay
    fetched_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ServiceAlertState:
    alert_id: str
    header: str
    description: str
    affected_route_ids: list[str] = field(default_factory=list)
    affected_stop_ids: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.utcnow)


# Module-level live state — read by the reliability layer
trip_updates: dict[str, TripUpdateState] = {}     # trip_id → state
service_alerts: list[ServiceAlertState] = []
vehicle_positions: dict[str, dict[str, Any]] = {} # trip_id → {lat, lon, timestamp}

_last_fetched: datetime | None = None

# Tracks trip_ids already recorded today — prevents double-counting across polls
_recorded_today: set[str] = set()
_recorded_date: str = ""   # YYYYMMDD; set resets when date changes


async def _fetch_feed(url: str) -> gtfs_realtime_pb2.FeedMessage | None:
    """Fetch and parse a GTFS-RT protobuf feed.

    Appends the API key as a ?key= query parameter when GTFS_RT_API_KEY is set.
    Uses httpx params= so the key is properly appended regardless of whether
    the URL already contains a query string.
    """
    if not url:
        return None
    params = {"key": GTFS_RT_API_KEY} if GTFS_RT_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params=params, headers={"Accept": "application/x-protobuf"})
            response.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)
        return feed
    except Exception as exc:
        logger.warning("Failed to fetch GTFS-RT feed %s: %s", url, exc)
        return None


async def poll_trip_updates() -> None:
    """Fetch trip updates and refresh the in-memory trip_updates dict."""
    feed = await _fetch_feed(GTFS_RT_TRIP_UPDATES_URL)
    if feed is None:
        return

    updated: dict[str, TripUpdateState] = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        route_id = tu.trip.route_id

        delay = 0
        cancelled = False
        overrides: dict[str, int] = {}

        for stu in tu.stop_time_update:
            stop_id = stu.stop_id
            if stu.HasField("departure"):
                overrides[stop_id] = stu.departure.delay
                delay = stu.departure.delay  # last known delay as overall proxy

        # GTFS-RT schedule_relationship 3 = CANCELED
        if tu.trip.schedule_relationship == 3:
            cancelled = True

        updated[trip_id] = TripUpdateState(
            trip_id=trip_id,
            route_id=route_id,
            delay_seconds=delay,
            is_cancelled=cancelled,
            stop_time_overrides=overrides,
        )

    trip_updates.clear()
    trip_updates.update(updated)
    logger.debug("Refreshed %d trip updates.", len(trip_updates))


async def poll_service_alerts() -> None:
    """Fetch service alerts and refresh the in-memory alerts list."""
    feed = await _fetch_feed(GTFS_RT_ALERTS_URL)
    if feed is None:
        return

    alerts: list[ServiceAlertState] = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        a = entity.alert
        route_ids = [
            ie.route_id for ie in a.informed_entity if ie.route_id
        ]
        stop_ids = [
            ie.stop_id for ie in a.informed_entity if ie.stop_id
        ]
        header = a.header_text.translation[0].text if a.header_text.translation else ""
        desc = a.description_text.translation[0].text if a.description_text.translation else ""
        alerts.append(ServiceAlertState(
            alert_id=entity.id,
            header=header,
            description=desc,
            affected_route_ids=route_ids,
            affected_stop_ids=stop_ids,
        ))

    service_alerts.clear()
    service_alerts.extend(alerts)
    logger.debug("Refreshed %d service alerts.", len(service_alerts))


async def poll_vehicle_positions() -> None:
    """Fetch vehicle positions and refresh the in-memory positions dict."""
    feed = await _fetch_feed(GTFS_RT_VEHICLE_POSITIONS_URL)
    if feed is None:
        return

    updated: dict[str, dict] = {}
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vp = entity.vehicle
        trip_id = vp.trip.trip_id
        updated[trip_id] = {
            "lat": vp.position.latitude,
            "lon": vp.position.longitude,
            "timestamp": vp.timestamp,
        }

    vehicle_positions.clear()
    vehicle_positions.update(updated)
    logger.debug("Refreshed %d vehicle positions.", len(vehicle_positions))


async def poll_all() -> None:
    """Poll all GTFS-RT feeds. Called by the scheduler.

    Skips all polling when GTFS_RT_API_KEY is not yet configured, so the
    rest of the system (static ingest, graph, routing) can run without
    a Metrolinx API key.
    """
    global _last_fetched
    if not GTFS_RT_API_KEY:
        logger.info(
            "GTFS-RT polling skipped — GTFS_RT_API_KEY not set. "
            "Static routing and historical scoring still available."
        )
        return
    await poll_trip_updates()
    await poll_service_alerts()
    await poll_vehicle_positions()
    _last_fetched = datetime.utcnow()
    logger.info("GTFS-RT poll complete at %s", _last_fetched.isoformat())


def _parse_scheduled_at(departure_time_str: str, service_id: str) -> datetime | None:
    """Convert GTFS departure_time string + service_id (YYYYMMDD) to a datetime.

    Handles GTFS times > 24:00:00 (post-midnight trips) via timedelta.
    """
    try:
        h, m, s = (int(x) for x in departure_time_str.split(":"))
        base = datetime.strptime(service_id, "%Y%m%d")
        return base + timedelta(hours=h, minutes=m, seconds=s)
    except (ValueError, AttributeError):
        return None


def observe_departures(session: Session) -> int:
    """
    Record observed/cancelled departures from current trip_updates into DB.

    Called after poll_all() to accumulate real reliability data.
    Each trip_id is processed at most once per service day to avoid
    double-counting across 30-second poll cycles.

    Returns the number of ReliabilityRecord updates written.
    """
    global _recorded_today, _recorded_date

    today = date.today().strftime("%Y%m%d")
    if today != _recorded_date:
        _recorded_today = set()
        _recorded_date = today

    unrecorded = {tid: state for tid, state in trip_updates.items()
                  if tid not in _recorded_today}
    if not unrecorded:
        return 0

    # Batch query: stop_id + departure_time + service_id for all unrecorded trips
    from db.models import StopTime, Trip
    rows = (
        session.query(StopTime.trip_id, StopTime.stop_id, StopTime.departure_time, Trip.service_id)
        .join(Trip, Trip.trip_id == StopTime.trip_id)
        .filter(StopTime.trip_id.in_(list(unrecorded)))
        .all()
    )

    # Group rows by trip_id
    stops_by_trip: dict[str, list] = defaultdict(list)
    for trip_id, stop_id, dep_time, service_id in rows:
        stops_by_trip[trip_id].append((stop_id, dep_time, service_id))

    now = datetime.utcnow()
    recorded = 0

    for trip_id, state in unrecorded.items():
        stop_rows = stops_by_trip.get(trip_id, [])
        if not stop_rows:
            continue  # not in static schedule — skip

        wrote_any = False

        if state.is_cancelled:
            # Record all stops as cancelled
            for stop_id, dep_time, service_id in stop_rows:
                scheduled_at = _parse_scheduled_at(dep_time, service_id)
                if scheduled_at is None:
                    continue
                record_observed_departure(
                    route_id=state.route_id,
                    stop_id=stop_id,
                    scheduled_at=scheduled_at,
                    delay_seconds=0,
                    was_cancelled=True,
                    session=session,
                )
                recorded += 1
                wrote_any = True

        elif state.stop_time_overrides:
            # Only record stops that have RT delay data AND whose scheduled
            # departure time has already passed (trip is underway)
            override_stops = set(state.stop_time_overrides.keys())
            for stop_id, dep_time, service_id in stop_rows:
                if stop_id not in override_stops:
                    continue
                scheduled_at = _parse_scheduled_at(dep_time, service_id)
                if scheduled_at is None or scheduled_at > now:
                    continue  # not yet departed — don't record yet
                delay = state.stop_time_overrides[stop_id]
                record_observed_departure(
                    route_id=state.route_id,
                    stop_id=stop_id,
                    scheduled_at=scheduled_at,
                    delay_seconds=delay,
                    was_cancelled=False,
                    session=session,
                )
                recorded += 1
                wrote_any = True

        if wrote_any:
            _recorded_today.add(trip_id)

    return recorded
