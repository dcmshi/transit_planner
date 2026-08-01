"""
Endpoint handlers (v1):
  GET  /routes?origin=<stop_id>&destination=<stop_id>&explain=<bool>
  GET  /stops?query=<name>
  GET  /alerts
  GET  /reliability?route_id=&stop_id=
  GET  /health
  POST /ingest/gtfs-static     (202 — background)
  GET  /ingest/status
  POST /ingest/reliability-seed

Handlers build plain dicts and return them; the `response_model=` on each
decorator is what validates and serialises the wire format.  The return
annotations say `dict[str, Any]` because that is what the functions actually
return — annotating them with the response model instead would be a lie
FastAPI happens to paper over.
"""

import asyncio
import logging
import secrets
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from api.cache import (
    _get_cached_routes,
    _inflight_lock_for,
    _release_inflight_lock,
    _routes_cache_key,
    _store_cached_routes,
)
from api.lifespan import (
    _ingest_state,
    _try_begin_ingest,
    scheduler,
    start_ingest_task,
)
from api.ratelimit import _rate_limit
from api.schemas import (
    AlertResult,
    HealthResponse,
    IngestResponse,
    IngestStatusResponse,
    ReliabilityResult,
    RoutesResponse,
    SeedResponse,
    StopResult,
)
from config import AGENCY_TZ, INGEST_API_KEY, MAX_ROUTES
from db.session import get_session
from graph.builder import get_graph, get_last_built_at
from gtfs_time import hms_to_seconds, seconds_to_hms
from ingestion.gtfs_realtime import get_rt_status, service_alerts
from ingestion.seed_reliability import seed_from_static
from llm.explainer import explain_routes
from reliability.historical import (
    NEUTRAL_PRIOR,
    classify_time_bucket,
    get_reliability_snapshots,
    score_record,
)
from reliability.live import compute_live_risk, get_live_delay, risk_label
from routing.engine import (
    count_transfers,
    dominates,
    find_routes,
    find_routes_arriving_by,
    route_metrics,
    total_travel_seconds,
    total_walk_metres,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ingest_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Hard ceiling on /reliability rows.  The endpoint exists to audit a
# route's score, not to export the table.
_RELIABILITY_MAX_LIMIT = 200


def _require_ingest_key(key: str | None = Security(_ingest_key_header)) -> None:
    """
    Optional API-key guard for the ingest endpoint.

    If INGEST_API_KEY is not set the endpoint is open (local dev / testing).
    If it is set, the request must include the matching X-API-Key header.
    """
    if not INGEST_API_KEY:
        return  # no key configured → open
    # Constant-time comparison — != leaks key length/prefix via timing.
    if key is None or not secrets.compare_digest(key, INGEST_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")


@router.get("/health", response_model=HealthResponse)
def health(session: Session = Depends(get_session)) -> dict[str, Any]:
    """
    Liveness + data-freshness check.

    Returns DB record counts, graph stats, and timestamps so operators can
    quickly tell whether GTFS data has been loaded and the graph is ready.
    """
    from sqlalchemy import func

    from config import GTFS_RT_API_KEY, GTFS_RT_POLL_SECONDS
    from db.models import ReliabilityRecord, Stop, Trip

    # GTFS data counts (0 if no data loaded yet)
    stop_count: int = session.query(func.count(Stop.stop_id)).scalar() or 0
    trip_count: int = session.query(func.count(Trip.trip_id)).scalar() or 0
    latest_service_date: str | None = session.query(func.max(Trip.service_id)).scalar()

    # Reliability records
    reliability_count: int = (
        session.query(func.count(ReliabilityRecord.id)).scalar() or 0
    )
    last_seeded_at: str | None = (
        session.query(func.max(ReliabilityRecord.updated_at)).scalar()
    )
    by_source: dict[str, int] = {
        source: count
        for source, count in session.query(
            ReliabilityRecord.source, func.count(ReliabilityRecord.id)
        ).group_by(ReliabilityRecord.source)
    }

    # Graph stats (may not be built yet)
    graph_built = False
    graph_nodes = 0
    graph_edges = 0
    last_built_at: str | None = None
    try:
        G = get_graph()
        graph_built = True
        graph_nodes = G.number_of_nodes()
        graph_edges = G.number_of_edges()
        ts = get_last_built_at()
        last_built_at = ts.isoformat() if ts else None
    except RuntimeError:
        pass

    # Next scheduled static refresh
    next_refresh_at: str | None = None
    daily_job = scheduler.get_job("daily_gtfs_refresh")
    if daily_job and daily_job.next_run_time:
        next_refresh_at = daily_job.next_run_time.isoformat()

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gtfs": {
            "stops": stop_count,
            "trips": trip_count,
            "latest_service_date": latest_service_date,
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "graph_built": graph_built,
            "last_built_at": last_built_at,
            "next_refresh_at": next_refresh_at,
        },
        "reliability": {
            "records": reliability_count,
            "last_seeded_at": last_seeded_at,
            "by_source": by_source,
        },
        "gtfs_rt": {
            "polling_active": GTFS_RT_API_KEY != "" and GTFS_RT_POLL_SECONDS > 0 and scheduler.running,
            "startup_fetch_only": GTFS_RT_API_KEY != "" and GTFS_RT_POLL_SECONDS == 0,
            **get_rt_status(),
        },
    }


@router.get("/stops", response_model=list[StopResult])
def search_stops(
    query: str = Query(..., min_length=2, max_length=128, description="Stop name substring to search"),
    session: Session = Depends(get_session),
    _: None = Depends(_rate_limit),
) -> list[dict[str, Any]]:
    """Search stops by name substring."""
    from collections import defaultdict

    from db.models import Stop, StopRoute

    # Escape LIKE wildcards so a stray % or _ in the user's query matches
    # literally instead of changing the pattern semantics.
    escaped = query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    results = (
        session.query(Stop)
        .filter(Stop.stop_name.ilike(f"%{escaped}%", escape="\\"))
        .limit(20)
        .all()
    )

    # Read the mapping materialised at ingest.  Deriving it here meant a
    # DISTINCT over the stop_times/trips join — ~72,000 rows scanned to
    # produce a few dozen pairs.
    stop_ids = [s.stop_id for s in results]
    route_rows = (
        session.query(StopRoute.stop_id, StopRoute.route_id)
        .filter(StopRoute.stop_id.in_(stop_ids))
        .all()
    )
    routes_by_stop: dict[str, list[str]] = defaultdict(list)
    for stop_id, route_id in route_rows:
        routes_by_stop[stop_id].append(route_id)

    return [
        {
            "stop_id": s.stop_id,
            "stop_name": s.stop_name,
            "lat": s.stop_lat,
            "lon": s.stop_lon,
            "routes_served": sorted(routes_by_stop[s.stop_id]),
        }
        for s in results
    ]


@router.get("/reliability", response_model=list[ReliabilityResult])
def get_reliability(
    route_id: str | None = Query(None, max_length=64, description="Filter by GTFS route_id"),
    stop_id: str | None = Query(None, max_length=64, description="Filter by GTFS stop_id"),
    time_bucket: str | None = Query(
        None, max_length=32,
        description="Filter by bucket, e.g. weekday_am_peak / weekday_pm_peak / "
                    "weekday_offpeak / weekend",
    ),
    limit: int = Query(50, ge=1, le=_RELIABILITY_MAX_LIMIT, description="Max records"),
    session: Session = Depends(get_session),
    _: None = Depends(_rate_limit),
) -> list[dict[str, Any]]:
    """
    Inspect the stored reliability counters behind a route's risk score.

    Read-only, for tuning and debugging: /health reports only aggregate
    counts by source, which is not enough to tell whether a route scores
    badly because of real observations or a synthetic prior.

    At least one of route_id or stop_id is required, and results are capped.
    This is a lookup tool, not a bulk export — pull the table directly if you
    need the whole thing.
    """
    from db.models import ReliabilityRecord

    if not route_id and not stop_id:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one of route_id or stop_id.",
        )

    query = session.query(ReliabilityRecord)
    if route_id:
        query = query.filter(ReliabilityRecord.route_id == route_id)
    if stop_id:
        query = query.filter(ReliabilityRecord.stop_id == stop_id)
    if time_bucket:
        query = query.filter(ReliabilityRecord.time_bucket == time_bucket)
    records = query.order_by(ReliabilityRecord.updated_at.desc()).limit(limit).all()

    results = []
    for r in records:
        score = score_record(r)
        results.append({
            "route_id": r.route_id,
            "stop_id": r.stop_id,
            "time_bucket": r.time_bucket,
            "source": r.source,
            "scheduled_departures": r.scheduled_departures or 0.0,
            "observed_departures": r.observed_departures or 0.0,
            "total_delay_seconds": r.total_delay_seconds or 0.0,
            "cancellation_count": r.cancellation_count or 0.0,
            "window_start_date": r.window_start_date,
            "window_end_date": r.window_end_date,
            "updated_at": r.updated_at,
            "score": score,
            "neutral_prior_used": score is None,
        })
    return results


@router.get("/alerts", response_model=list[AlertResult])
def get_alerts(_: None = Depends(_rate_limit)) -> list[dict[str, Any]]:
    """Active GTFS-RT service alerts — lets a frontend show a disruption
    banner without requesting routes.  Empty until RT polling is active."""
    # list(...) snapshot: this sync endpoint runs in a worker thread while
    # the poller clears/extends the shared list on the event loop.
    return [
        {
            "alert_id": a.alert_id,
            "header": a.header,
            "description": a.description,
            "affected_route_ids": a.affected_route_ids,
            "affected_stop_ids": a.affected_stop_ids,
            "fetched_at": a.fetched_at.isoformat(),
        }
        for a in list(service_alerts)
    ]


def _parse_arrive_by(value: str) -> int:
    """Parse an arrive_by query value to seconds past midnight.

    Accepts GTFS-style hours past 23 (25:30 = 01:30 next morning), which is
    why this returns seconds rather than a datetime.
    """
    parts = value.split(":")
    try:
        hours, minutes = int(parts[0]), int(parts[1])
        seconds = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        raise HTTPException(
            status_code=422, detail=f"Invalid arrive_by value: {value!r}. Use HH:MM or HH:MM:SS."
        )
    if not (0 <= hours <= 47 and 0 <= minutes < 60 and 0 <= seconds < 60):
        raise HTTPException(
            status_code=422, detail=f"arrive_by out of range: {value!r}."
        )
    return hours * 3600 + minutes * 60 + seconds


def _prune_dominated(
    scored_routes: list[dict[str, Any]], latest_departure_first: bool = False
) -> list[dict[str, Any]]:
    """
    Drop routes strictly worse than another on every axis, then sort by
    arrival time.

    Route B is dominated by A when A departs no earlier (less waiting at
    the origin), arrives no later, has no more transfers, walks no
    further, and is no riskier — with at least one axis strictly better.
    Walking is an axis so a heavy-walk option can never silently delete a
    zero-walk alternative that a rider might prefer.  Yen's + the
    later-departure fill can produce e.g. four options that all leave with
    option #1 but arrive hours later with two extra transfers; showing
    them helps no rider.  Ties on every axis keep both routes.

    Routes without trip legs (filtered upstream, handled defensively) are
    incomparable — always kept, appended last.

    routing.engine already prunes on the four axes it can measure while it
    builds its route budget, so most of the work is done by the time a route
    gets here.  This pass exists for the axis the engine cannot see: risk
    needs historical reliability and live GTFS-RT state, so it only becomes
    comparable after scoring.  The metric tuple extends the engine's, and the
    comparison is the engine's `dominates`, so the two can never disagree
    about what "worse on every axis" means.
    """
    Metrics = tuple[float, ...]
    comparable: list[tuple[Metrics, dict[str, Any]]] = []
    incomparable: list[dict[str, Any]] = []
    for route in scored_routes:
        base = route_metrics(route["legs"], route.get("total_walk_metres", 0.0))
        if base is None:
            incomparable.append(route)
            continue
        comparable.append((base + (route["risk_score"],), route))

    survivors: list[tuple[Metrics, dict[str, Any]]] = [
        (m_i, route)
        for i, (m_i, route) in enumerate(comparable)
        if not any(
            dominates(m_j, m_i)
            for j, (m_j, _r) in enumerate(comparable)
            if j != i
        )
    ]

    # Earliest arrival first (ties by risk, then transfers) — Yen's path
    # weight is not a meaningful presentation order for riders.  Under an
    # arrive-by query every survivor already meets the deadline, so the useful
    # order is instead the one that lets the rider leave latest.
    # Metrics are (-departure, arrival, transfers, walk, risk); departure is
    # already negated, so ascending on it is latest-first.
    if latest_departure_first:
        survivors.sort(key=lambda mr: (mr[0][0], mr[0][4], mr[0][2]))
    else:
        survivors.sort(key=lambda mr: (mr[0][1], mr[0][4], mr[0][2]))
    return [route for _m, route in survivors] + incomparable


def _score_routes_blocking(
    origin: str,
    destination: str,
    departure_dt: datetime,
    session: Session,
    arrive_by_sec: int | None = None,
) -> list[dict[str, Any]]:
    """
    Blocking part of GET /routes: cache lookup, route generation, and risk
    scoring.  Called via asyncio.to_thread so the event loop stays free.
    Raises HTTPException on routing failures (propagates through the await).

    With arrive_by_sec set, departure_dt supplies only the travel date and the
    search runs backwards from that deadline instead.
    """
    # The deadline belongs in the key.  Under arrive_by the caller's
    # departure_dt carries only the travel date, so without it every deadline
    # for one origin/destination/date collapsed onto a single entry — the
    # first answer was then served for every other deadline, including ones it
    # arrives after, and a legitimately empty result negative-cached 404s over
    # deadlines that do have service.
    deadline_missed = False
    mode = "depart" if arrive_by_sec is None else f"arrive:{arrive_by_sec}"
    cache_key = _routes_cache_key(origin, destination, departure_dt, mode)
    routes = _get_cached_routes(cache_key)
    if routes is None:
        key_lock = _inflight_lock_for(cache_key)
        try:
            with key_lock:
                # Re-check — another request may have filled the cache while
                # this one waited on the lock.
                routes = _get_cached_routes(cache_key)
                if routes is None:
                    try:
                        if arrive_by_sec is None:
                            routes = find_routes(
                                origin, destination,
                                departure_dt=departure_dt,
                                session=session,
                                max_routes=MAX_ROUTES,
                            )
                        else:
                            found = find_routes_arriving_by(
                                origin, destination,
                                arrive_by_sec=arrive_by_sec,
                                travel_day=departure_dt.date(),
                                session=session,
                                max_routes=MAX_ROUTES,
                            )
                            routes = found.routes
                            # Service exists, nothing reaches the destination
                            # in time.  Raising here would be caught by the
                            # broad handler below and surface as a 500.
                            deadline_missed = not routes and found.any_service
                    except ValueError as exc:
                        raise HTTPException(status_code=404, detail=str(exc))
                    except Exception as exc:
                        raise HTTPException(status_code=500, detail=f"Routing error: {exc}")
                    # Empty results are stored too (negative cache) so
                    # repeated unroutable queries don't re-run Yen's — except
                    # a missed deadline, which is not cached so that a cached
                    # empty result always means "no service" and the two 404s
                    # never blur together on a cache hit.
                    if not deadline_missed:
                        _store_cached_routes(cache_key, routes)
        finally:
            _release_inflight_lock(cache_key)

    if not routes:
        if deadline_missed and arrive_by_sec is not None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No route arrives by {seconds_to_hms(arrive_by_sec)} "
                    "— try a later deadline."
                ),
            )
        raise HTTPException(status_code=404, detail="No routes found between these stops.")

    # Agency-local naive wall clock — the same frame as schedule times.
    query_dt = datetime.now(AGENCY_TZ).replace(tzinfo=None)
    travel_day = departure_dt.date()

    def _leg_dt(leg: dict[str, Any]) -> datetime:
        # The leg's scheduled departure on the travel date — GTFS times may
        # exceed 24:00:00, so timedelta rolls into the next day.  Risk is
        # keyed to when the bus runs, not when the query is made.
        return datetime(travel_day.year, travel_day.month, travel_day.day) + timedelta(
            seconds=hms_to_seconds(leg["departure_time"])
        )

    # One historical-reliability query for every trip leg in the response
    # (up to MAX_ROUTES × legs point queries otherwise).  Snapshots rather
    # than bare scores so each leg can carry the counters behind its risk.
    snapshots = get_reliability_snapshots(
        [
            (leg["route_id"], leg["from_stop_id"], classify_time_bucket(_leg_dt(leg)))
            for route_legs in routes
            for leg in route_legs
            if leg["kind"] == "trip"
        ],
        session,
    )

    scored_routes: list[dict[str, Any]] = []
    for route_legs in routes:
        scored_legs = []
        route_risk_scores = []

        for leg in route_legs:
            if leg["kind"] != "trip":
                scored_legs.append(leg)
                continue

            leg_dt = _leg_dt(leg)
            snapshot = snapshots.get(
                (leg["route_id"], leg["from_stop_id"], classify_time_bucket(leg_dt))
            )
            hist = (
                snapshot.score
                if snapshot is not None and snapshot.score is not None
                else NEUTRAL_PRIOR
            )
            live = compute_live_risk(
                route_id=leg["route_id"],
                stop_id=leg["from_stop_id"],
                trip_id=leg["trip_id"],
                departure_time_str=leg["departure_time"],
                query_dt=query_dt,
                historical_reliability=hist,
                scheduled_dt=leg_dt,
                service_date=travel_day,
                reliability=snapshot,
            )
            scored_leg = {**leg, "risk": live}
            # Live expected times — same SERVICE day only (a >24:00:00 leg
            # rolls leg_dt onto tomorrow but belongs to today's run).
            if travel_day == query_dt.date():
                delay = get_live_delay(leg["trip_id"], leg["from_stop_id"])
                if delay:
                    scored_leg["live_delay_seconds"] = delay
                    scored_leg["expected_departure"] = seconds_to_hms(
                        hms_to_seconds(leg["departure_time"]) + delay
                    )
                    scored_leg["expected_arrival"] = seconds_to_hms(
                        hms_to_seconds(leg["arrival_time"]) + delay
                    )
            scored_legs.append(scored_leg)
            route_risk_scores.append(live["risk_score"])

        overall_risk = max(route_risk_scores) if route_risk_scores else 0.0

        scored_routes.append({
            "legs": scored_legs,
            "total_travel_seconds": total_travel_seconds(route_legs),
            "transfers": count_transfers(route_legs),
            "total_walk_metres": round(total_walk_metres(route_legs), 1),
            "risk_score": round(overall_risk, 3),
            "risk_label": risk_label(overall_risk),
        })

    return _prune_dominated(scored_routes, latest_departure_first=arrive_by_sec is not None)


@router.get("/routes", response_model=RoutesResponse)
async def get_routes(
    origin: str = Query(..., max_length=64, description="Origin stop_id"),
    destination: str = Query(..., max_length=64, description="Destination stop_id"),
    departure_time: str | None = Query(
        None,
        max_length=8,
        description="Earliest departure time as HH:MM or HH:MM:SS. Defaults to current time.",
    ),
    travel_date: str | None = Query(
        None,
        max_length=10,
        description="Travel date as YYYY-MM-DD. Defaults to today.",
    ),
    arrive_by: str | None = Query(
        None,
        max_length=8,
        description=(
            "Latest acceptable arrival as HH:MM or HH:MM:SS, returning the "
            "latest-departing options that still make it. GTFS convention "
            "applies, so 25:30 means 01:30 the next morning. Cannot be "
            "combined with departure_time."
        ),
    ),
    explain: bool = Query(False, description="Include LLM plain-language explanation"),
    session: Session = Depends(get_session),
    _: None = Depends(_rate_limit),
) -> dict[str, Any]:
    """
    Return top-N scored routes from origin to destination.

    Routes have real scheduled departure/arrival times for the requested date
    and time.  Optionally include an LLM-generated explanation of tradeoffs.
    """
    if origin == destination:
        raise HTTPException(
            status_code=422,
            detail="Origin and destination must be different stops.",
        )
    if arrive_by and departure_time:
        raise HTTPException(
            status_code=422,
            detail="Specify either departure_time or arrive_by, not both.",
        )

    # Parse departure datetime, defaulting to now in the agency's timezone.
    # departure_dt stays naive agency-local wall clock — the same frame as
    # GTFS schedule times.
    try:
        base_date = Date.fromisoformat(travel_date) if travel_date else datetime.now(AGENCY_TZ).date()
        if departure_time:
            parts = departure_time.split(":")
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
            departure_dt = datetime(base_date.year, base_date.month, base_date.day, h, m, s)
        else:
            now = datetime.now(AGENCY_TZ)
            departure_dt = datetime(base_date.year, base_date.month, base_date.day,
                                    now.hour, now.minute, now.second)
    except (ValueError, IndexError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date/time parameter: {exc}")

    # Kept as seconds past midnight rather than a datetime: GTFS deadlines may
    # exceed 24:00:00, which datetime cannot represent.
    arrive_by_sec: int | None = None
    if arrive_by:
        arrive_by_sec = _parse_arrive_by(arrive_by)
        # departure_dt then carries only the travel date.
        departure_dt = datetime(base_date.year, base_date.month, base_date.day)

    # Routing and risk scoring are sync DB/CPU work; run off the event loop
    # so a slow request doesn't stall concurrent ones.
    scored_routes = await asyncio.to_thread(
        _score_routes_blocking, origin, destination, departure_dt, session, arrive_by_sec
    )

    response: dict[str, Any] = {"routes": scored_routes}

    if explain:
        alerts_payload = [
            {"header": a.header, "description": a.description,
             "routes": a.affected_route_ids, "stops": a.affected_stop_ids}
            for a in list(service_alerts)  # snapshot vs poller mutation
        ]
        G = get_graph()
        origin_name = G.nodes[origin].get("name", origin) if origin in G else origin
        dest_name = G.nodes[destination].get("name", destination) if destination in G else destination

        response["explanation"] = await explain_routes(
            routes_with_scores=scored_routes,
            active_alerts=alerts_payload,
            origin_name=origin_name,
            destination_name=dest_name,
        )

    return response


@router.post("/ingest/gtfs-static", response_model=IngestResponse, status_code=202)
async def trigger_gtfs_ingest(
    _: None = Depends(_require_ingest_key),
) -> dict[str, Any]:
    """
    Trigger a GTFS static data refresh, graph rebuild, and reliability
    reseed in the background.  (In production this also runs on a daily
    schedule.)

    Returns 202 immediately — the full ingest takes ~60 s.  Poll
    GET /ingest/status (or /health) for completion.  409 if an ingest is
    already running.
    """
    if not _try_begin_ingest():
        raise HTTPException(
            status_code=409,
            detail="An ingest is already running — poll GET /ingest/status.",
        )
    start_ingest_task()
    return {
        "status": "accepted",
        "message": "GTFS ingest started in the background — poll GET /ingest/status.",
    }


@router.get("/ingest/status", response_model=IngestStatusResponse)
def ingest_status(_: None = Depends(_require_ingest_key)) -> dict[str, Any]:
    """State of the current/most recent ingest (manual or daily refresh)."""
    return dict(_ingest_state)


@router.post("/ingest/reliability-seed", response_model=SeedResponse)
def trigger_reliability_seed(
    window_days: int = Query(14, ge=1, le=90, description="Days of schedule to sample"),
    session: Session = Depends(get_session),
    _: None = Depends(_require_ingest_key),
) -> dict[str, Any]:
    """
    Seed reliability_records from the static GTFS schedule.

    Uses synthetic per-bucket reliability priors (no GTFS-RT required).
    Safe to call repeatedly — existing records are overwritten.
    POST /ingest/gtfs-static already reseeds; call this only to re-seed
    with a different window_days sample.
    """
    try:
        written = seed_from_static(session, window_days=window_days)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "status": "ok",
        "records_written": written,
        "message": f"Seeded {written} reliability records from {window_days}-day schedule window.",
    }
