"""
Polls GO Transit GTFS-Realtime feeds and exposes their latest state.

Feeds polled:
  - Trip Updates   (delays, cancellations, stop-time overrides)
  - Vehicle Positions (optional — locate buses in real time)
  - Service Alerts (human-readable disruption notices)

The parsed state is held in-memory in module-level dicts and updated
every GTFS_RT_POLL_SECONDS seconds by the scheduler started at API boot.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from google.transit import gtfs_realtime_pb2
from sqlalchemy import text
from sqlalchemy.orm import Session

from config import (
    AGENCY_TZ,
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
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ServiceAlertState:
    alert_id: str
    header: str
    description: str
    affected_route_ids: list[str] = field(default_factory=list)
    affected_stop_ids: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Module-level live state — read by the reliability layer
trip_updates: dict[str, TripUpdateState] = {}     # trip_id → state
service_alerts: list[ServiceAlertState] = []
vehicle_positions: dict[str, dict[str, Any]] = {} # trip_id → {lat, lon, timestamp}

_last_fetched: datetime | None = None

# Tracks trip_ids already recorded today — prevents double-counting across polls
_recorded_today: set[str] = set()
_recorded_date: str = ""   # YYYYMMDD; set resets when date changes

# No-show detection state.
# _seen_in_rt_today accumulates every trip_id that appeared in any RT feed
# (trip updates or vehicle positions) since the last date rollover — the RT
# snapshot dicts only hold the current poll, so without this a trip that ran
# at 10:00 would look "never seen" by 11:00.
# _polling_since marks the start of continuous RT coverage: set on the first
# successful poll, reset to None when all feeds fail (a coverage hole means
# a trip could have shown evidence we never saw — don't judge those).
_seen_in_rt_today: set[str] = set()
_polling_since: datetime | None = None       # UTC
_last_noshow_sweep: datetime | None = None   # UTC; throttles the sweep
NO_SHOW_GRACE_MINUTES = 30
NO_SHOW_SWEEP_SECONDS = 300

# Exponential backoff for sustained API failures
_consecutive_poll_failures: int = 0
_backoff_until: datetime | None = None
_MAX_BACKOFF_SECONDS: int = 1800  # cap at 30 minutes


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


async def poll_trip_updates() -> bool:
    """Fetch trip updates and refresh the in-memory trip_updates dict.

    Returns True on success, False if the feed could not be fetched.
    """
    feed = await _fetch_feed(GTFS_RT_TRIP_UPDATES_URL)
    if feed is None:
        return False

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
    return True


async def poll_service_alerts() -> bool:
    """Fetch service alerts and refresh the in-memory alerts list.

    Returns True on success, False if the feed could not be fetched.
    """
    feed = await _fetch_feed(GTFS_RT_ALERTS_URL)
    if feed is None:
        return False

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
    return True


async def poll_vehicle_positions() -> bool:
    """Fetch vehicle positions and refresh the in-memory positions dict.

    Returns True on success, False if the feed could not be fetched.
    """
    feed = await _fetch_feed(GTFS_RT_VEHICLE_POSITIONS_URL)
    if feed is None:
        return False

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
    return True


async def poll_all() -> None:
    """Poll all GTFS-RT feeds. Called by the scheduler.

    Skips all polling when GTFS_RT_API_KEY is not yet configured, so the
    rest of the system (static ingest, graph, routing) can run without
    a Metrolinx API key.

    Implements exponential backoff: if all three feeds fail, the next
    poll is skipped until _backoff_until has elapsed.  Backoff doubles
    on each consecutive failure, capped at _MAX_BACKOFF_SECONDS (30 min).
    A single successful poll resets the backoff counter.
    """
    global _last_fetched, _consecutive_poll_failures, _backoff_until, _polling_since

    if not GTFS_RT_API_KEY:
        logger.info(
            "GTFS-RT polling skipped — GTFS_RT_API_KEY not set. "
            "Static routing and historical scoring still available."
        )
        return

    now = datetime.now(timezone.utc)
    if _backoff_until is not None and now < _backoff_until:
        logger.debug(
            "GTFS-RT poll skipped — backing off until %s (%d consecutive failures).",
            _backoff_until.isoformat(), _consecutive_poll_failures,
        )
        return

    results = await asyncio.gather(
        poll_trip_updates(),
        poll_service_alerts(),
        poll_vehicle_positions(),
    )

    if any(results):
        # At least one feed succeeded — reset backoff
        _consecutive_poll_failures = 0
        _backoff_until = None
        _last_fetched = datetime.now(timezone.utc)
        if _polling_since is None:
            _polling_since = _last_fetched
        # Remember every trip that has shown RT evidence today — the
        # snapshot dicts only hold the current poll (see record_no_shows).
        _seen_in_rt_today.update(trip_updates.keys())
        _seen_in_rt_today.update(vehicle_positions.keys())
        logger.debug("GTFS-RT poll complete at %s", _last_fetched.isoformat())
    else:
        # All three feeds failed — coverage hole; no-show judgements must
        # restart from the next successful poll.
        _polling_since = None
        _consecutive_poll_failures += 1
        backoff_secs = min(60 * (2 ** (_consecutive_poll_failures - 1)), _MAX_BACKOFF_SECONDS)
        _backoff_until = datetime.now(timezone.utc) + timedelta(seconds=backoff_secs)
        logger.warning(
            "All GTFS-RT feeds failed (failure #%d). Next poll not before %s.",
            _consecutive_poll_failures, _backoff_until.isoformat(),
        )


def get_rt_status() -> dict[str, Any]:
    """Snapshot of RT polling health, surfaced on /health so an operator
    can tell the feeds have been failing without reading logs."""
    return {
        "last_fetched_at": _last_fetched.isoformat() if _last_fetched else None,
        "consecutive_failures": _consecutive_poll_failures,
        "backing_off_until": _backoff_until.isoformat() if _backoff_until else None,
        "polling_coverage_since": _polling_since.isoformat() if _polling_since else None,
        "trip_updates": len(trip_updates),
        "service_alerts": len(service_alerts),
        "vehicle_positions": len(vehicle_positions),
    }


def _parse_scheduled_at(
    departure_time_str: str, service_id: str, trip_id: str = ""
) -> datetime | None:
    """Convert GTFS departure_time string + service_id (YYYYMMDD) to a datetime.

    GTFS times are agency-local wall clock, so the result is anchored to
    AGENCY_TZ (aware) — comparing it against UTC "now" then converts
    correctly.  Handles GTFS times > 24:00:00 (post-midnight trips) via
    timedelta on the naive wall clock before attaching the zone.
    Returns None (and logs a warning) on malformed input — the caller skips
    the observation, so silence here would hide dropped data.
    """
    try:
        h, m, s = (int(x) for x in departure_time_str.split(":"))
        naive = datetime.strptime(service_id, "%Y%m%d") + timedelta(hours=h, minutes=m, seconds=s)
        return naive.replace(tzinfo=AGENCY_TZ)
    except (ValueError, AttributeError):
        logger.warning(
            "_parse_scheduled_at: could not parse departure_time=%r service_id=%r "
            "(trip_id=%s) — observation skipped",
            departure_time_str, service_id, trip_id,
        )
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

    from db.models import ObservedTrip, StopTime, Trip

    # Service days roll over at agency-local midnight, not UTC midnight.
    today = datetime.now(AGENCY_TZ).date().strftime("%Y%m%d")
    if today != _recorded_date:
        # Stale or empty in-memory state (new day, or fresh process after a
        # restart) — reload today's dedup markers from the DB so already
        # recorded trips are not double-counted.
        _seen_in_rt_today.clear()  # yesterday's RT evidence is irrelevant now
        _recorded_today = {
            trip_id
            for (trip_id,) in session.query(ObservedTrip.trip_id)
            .filter(ObservedTrip.recorded_date == today)
        }
        stale = (
            session.query(ObservedTrip)
            .filter(ObservedTrip.recorded_date < today)
            .delete()
        )
        if stale:
            session.commit()
        _recorded_date = today

    unrecorded = {tid: state for tid, state in trip_updates.items()
                  if tid not in _recorded_today}
    if not unrecorded:
        return 0

    # Batch query: stop_id + departure_time + service_id for all unrecorded trips
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

    now = datetime.now(timezone.utc)
    recorded = 0
    newly_recorded: set[str] = set()

    for trip_id, state in unrecorded.items():
        stop_rows = stops_by_trip.get(trip_id, [])
        if not stop_rows:
            continue  # not in static schedule — skip

        wrote_any = False

        if state.is_cancelled:
            # Record all stops as cancelled
            for stop_id, dep_time, service_id in stop_rows:
                scheduled_at = _parse_scheduled_at(dep_time, service_id, trip_id)
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
                scheduled_at = _parse_scheduled_at(dep_time, service_id, trip_id)
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
            newly_recorded.add(trip_id)

    if newly_recorded:
        # One commit per poll cycle instead of one per observation.  Dedup
        # markers are persisted in the same transaction, and trips are marked
        # recorded in memory only after the commit succeeds, so a failed
        # cycle rolls back atomically and is retried on the next poll.
        for trip_id in newly_recorded:
            session.add(ObservedTrip(trip_id=trip_id, recorded_date=today))
        session.commit()
        _recorded_today |= newly_recorded

    return recorded


# GTFS HH:MM:SS → seconds-past-midnight, in SQL (works on SQLite and
# PostgreSQL; the routing engine uses the same expression).
_DEP_SEC_SQL = (
    "CAST(substr(st.departure_time, 1, 2) AS INT) * 3600"
    " + CAST(substr(st.departure_time, 4, 2) AS INT) * 60"
    " + CAST(substr(st.departure_time, 7, 2) AS INT)"
)


def record_no_shows(session: Session) -> int:
    """
    Record scheduled trips that never appeared in any GTFS-RT feed as misses.

    This is the signal observe_departures cannot capture: a bus that silently
    never runs produces no TripUpdate at all, so nothing was ever recorded
    and observed_rate never dropped.  A trip counts as a no-show when:

      - its entire scheduled run happened inside the continuous RT coverage
        window (first departure after _polling_since — trips that started
        before we were watching are not judged),
      - the grace period after its final scheduled departure has elapsed,
      - it never appeared in trip updates or vehicle positions today, and
      - it was not already recorded (observed or cancelled).

    Each stop of the trip is recorded with was_missed=True (scheduled += 1,
    observed += 0).  Runs at most once per NO_SHOW_SWEEP_SECONDS.

    Trips with post-midnight (>24:00:00) final departures are never swept —
    their service day ends before they can satisfy the cutoff.  This is a
    known small gap, acceptable for the Toronto–Guelph corridor where
    service ends before midnight.

    Returns the number of missed departures recorded.
    """
    global _last_noshow_sweep, _recorded_today

    from db.models import ObservedTrip, StopTime, Trip

    if _polling_since is None:
        return 0
    now_utc = datetime.now(timezone.utc)
    if (
        _last_noshow_sweep is not None
        and (now_utc - _last_noshow_sweep).total_seconds() < NO_SHOW_SWEEP_SECONDS
    ):
        return 0
    _last_noshow_sweep = now_utc

    now_local = now_utc.astimezone(AGENCY_TZ)
    today = now_local.strftime("%Y%m%d")
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    now_sec = (now_local - midnight).total_seconds()
    cutoff_sec = now_sec - NO_SHOW_GRACE_MINUTES * 60

    polling_local = _polling_since.astimezone(AGENCY_TZ)
    coverage_start_sec = (
        0.0 if polling_local < midnight else (polling_local - midnight).total_seconds()
    )

    candidates = session.execute(
        text(f"""
            SELECT t.trip_id, t.route_id
            FROM trips t
            JOIN stop_times st ON st.trip_id = t.trip_id
            WHERE t.service_id = :today
            GROUP BY t.trip_id, t.route_id
            HAVING MAX({_DEP_SEC_SQL}) <= :cutoff
               AND MIN({_DEP_SEC_SQL}) >= :coverage_start
        """),
        {"today": today, "cutoff": cutoff_sec, "coverage_start": coverage_start_sec},
    ).fetchall()

    route_by_trip = {
        trip_id: route_id
        for trip_id, route_id in candidates
        if trip_id not in _recorded_today and trip_id not in _seen_in_rt_today
    }
    if not route_by_trip:
        return 0

    stop_rows = (
        session.query(StopTime.trip_id, StopTime.stop_id, StopTime.departure_time)
        .filter(StopTime.trip_id.in_(list(route_by_trip)))
        .all()
    )

    recorded = 0
    newly_recorded: set[str] = set()
    for trip_id, stop_id, dep_time in stop_rows:
        scheduled_at = _parse_scheduled_at(dep_time, today, trip_id)
        if scheduled_at is None:
            continue
        record_observed_departure(
            route_id=route_by_trip[trip_id],
            stop_id=stop_id,
            scheduled_at=scheduled_at,
            delay_seconds=0,
            was_cancelled=False,
            session=session,
            was_missed=True,
        )
        recorded += 1
        newly_recorded.add(trip_id)

    if newly_recorded:
        # Same commit discipline as observe_departures: markers persist in
        # the same transaction; in-memory state updates only after commit.
        for trip_id in newly_recorded:
            session.add(ObservedTrip(trip_id=trip_id, recorded_date=today))
        session.commit()
        _recorded_today |= newly_recorded
        logger.info(
            "No-show sweep: %d trips with no RT evidence recorded as missed (%d departures).",
            len(newly_recorded), recorded,
        )

    return recorded
