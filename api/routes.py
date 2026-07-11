"""
Endpoint handlers (v1):
  GET  /routes?origin=<stop_id>&destination=<stop_id>&explain=<bool>
  GET  /stops?query=<name>
  GET  /alerts
  GET  /health
  POST /ingest/gtfs-static     (202 — background)
  GET  /ingest/status
  POST /ingest/reliability-seed
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
    get_historical_reliability_batch,
)
from reliability.live import compute_live_risk, get_live_delay
from routing.engine import (
    count_transfers,
    find_routes,
    total_travel_seconds,
    total_walk_metres,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ingest_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


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
def health(session: Session = Depends(get_session)) -> HealthResponse:
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
) -> list[StopResult]:
    """Search stops by name substring."""
    from collections import defaultdict

    from db.models import Stop, StopTime, Trip

    # Escape LIKE wildcards so a stray % or _ in the user's query matches
    # literally instead of changing the pattern semantics.
    escaped = query.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    results = (
        session.query(Stop)
        .filter(Stop.stop_name.ilike(f"%{escaped}%", escape="\\"))
        .limit(20)
        .all()
    )

    # Fetch distinct route_ids for all matching stops in one query.
    stop_ids = [s.stop_id for s in results]
    route_rows = (
        session.query(StopTime.stop_id, Trip.route_id)
        .join(Trip, Trip.trip_id == StopTime.trip_id)
        .filter(StopTime.stop_id.in_(stop_ids))
        .distinct()
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


@router.get("/alerts", response_model=list[AlertResult])
def get_alerts(_: None = Depends(_rate_limit)) -> list[AlertResult]:
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


def _prune_dominated(scored_routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    """
    Metrics = tuple[int, int, int, float, float]
    comparable: list[tuple[Metrics, dict[str, Any]]] = []
    incomparable: list[dict[str, Any]] = []
    for route in scored_routes:
        trip_legs = [leg for leg in route["legs"] if leg["kind"] == "trip"]
        if not trip_legs:
            incomparable.append(route)
            continue
        comparable.append((
            (
                hms_to_seconds(trip_legs[0]["departure_time"]),
                hms_to_seconds(trip_legs[-1]["arrival_time"]),
                route["transfers"],
                route["risk_score"],
                route.get("total_walk_metres", 0.0),
            ),
            route,
        ))

    survivors: list[tuple[Metrics, dict[str, Any]]] = []
    for i, (m_i, route) in enumerate(comparable):
        dep_i, arr_i, tr_i, risk_i, walk_i = m_i
        dominated = any(
            dep_j >= dep_i and arr_j <= arr_i and tr_j <= tr_i
            and risk_j <= risk_i and walk_j <= walk_i
            and (dep_j > dep_i or arr_j < arr_i or tr_j < tr_i
                 or risk_j < risk_i or walk_j < walk_i)
            for j, ((dep_j, arr_j, tr_j, risk_j, walk_j), _r) in enumerate(comparable)
            if j != i
        )
        if not dominated:
            survivors.append((m_i, route))

    # Earliest arrival first (ties by risk, then transfers) — Yen's path
    # weight is not a meaningful presentation order for riders.
    survivors.sort(key=lambda mr: (mr[0][1], mr[0][3], mr[0][2]))
    return [route for _m, route in survivors] + incomparable


def _score_routes_blocking(
    origin: str,
    destination: str,
    departure_dt: datetime,
    session: Session,
) -> list[dict[str, Any]]:
    """
    Blocking part of GET /routes: cache lookup, route generation, and risk
    scoring.  Called via asyncio.to_thread so the event loop stays free.
    Raises HTTPException on routing failures (propagates through the await).
    """
    cache_key = _routes_cache_key(origin, destination, departure_dt)
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
                        routes = find_routes(
                            origin, destination,
                            departure_dt=departure_dt,
                            session=session,
                            max_routes=MAX_ROUTES,
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=404, detail=str(exc))
                    except Exception as exc:
                        raise HTTPException(status_code=500, detail=f"Routing error: {exc}")
                    # Empty results are stored too (negative cache) so
                    # repeated unroutable queries don't re-run Yen's.
                    _store_cached_routes(cache_key, routes)
        finally:
            _release_inflight_lock(cache_key)

    if not routes:
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
    # (up to MAX_ROUTES × legs point queries otherwise).
    hist_by_key = get_historical_reliability_batch(
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
            hist = hist_by_key.get(
                (leg["route_id"], leg["from_stop_id"], classify_time_bucket(leg_dt)),
                NEUTRAL_PRIOR,
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
        risk_label = "Low" if overall_risk < 0.33 else "Medium" if overall_risk < 0.66 else "High"

        scored_routes.append({
            "legs": scored_legs,
            "total_travel_seconds": total_travel_seconds(route_legs),
            "transfers": count_transfers(route_legs),
            "total_walk_metres": round(total_walk_metres(route_legs), 1),
            "risk_score": round(overall_risk, 3),
            "risk_label": risk_label,
        })

    return _prune_dominated(scored_routes)


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
    explain: bool = Query(False, description="Include LLM plain-language explanation"),
    session: Session = Depends(get_session),
    _: None = Depends(_rate_limit),
) -> RoutesResponse:
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

    # Routing and risk scoring are sync DB/CPU work; run off the event loop
    # so a slow request doesn't stall concurrent ones.
    scored_routes = await asyncio.to_thread(
        _score_routes_blocking, origin, destination, departure_dt, session
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
) -> IngestResponse:
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
def ingest_status(_: None = Depends(_require_ingest_key)) -> IngestStatusResponse:
    """State of the current/most recent ingest (manual or daily refresh)."""
    return dict(_ingest_state)


@router.post("/ingest/reliability-seed", response_model=SeedResponse)
def trigger_reliability_seed(
    window_days: int = Query(14, ge=1, le=90, description="Days of schedule to sample"),
    session: Session = Depends(get_session),
    _: None = Depends(_require_ingest_key),
) -> SeedResponse:
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
