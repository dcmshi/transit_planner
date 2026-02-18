"""
FastAPI application entry point.

On startup:
  1. Initialise the database schema.
  2. Build the transit graph from stored GTFS data (if available).
  3. Start the APScheduler:
       - Daily GTFS static refresh + graph rebuild + reliability reseed
         (every GTFS_REFRESH_HOURS, default 24h — always active).
       - GTFS-RT polling every GTFS_RT_POLL_SECONDS
         (only when GTFS_RT_API_KEY is set).

Endpoints (v1):
  GET  /routes?origin=<stop_id>&destination=<stop_id>&explain=<bool>
  GET  /stops?query=<name>
  GET  /health
  POST /ingest/gtfs-static
  POST /ingest/reliability-seed
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, date as Date, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from api.schemas import (
    HealthResponse,
    IngestResponse,
    RoutesResponse,
    SeedResponse,
    StopResult,
)
from config import CORS_ORIGINS, GTFS_REFRESH_HOURS, GTFS_RT_API_KEY, GTFS_RT_POLL_SECONDS, INGEST_API_KEY, MAX_ROUTES
from db.session import SessionLocal, get_session, init_db
from graph.builder import build_graph, get_graph, get_last_built_at
from ingestion.gtfs_realtime import poll_all
from ingestion.gtfs_static import refresh_static_data
from ingestion.seed_reliability import seed_from_static
from llm.explainer import explain_routes
from reliability.historical import classify_time_bucket, get_historical_reliability
from reliability.live import compute_live_risk
from routing.engine import count_transfers, find_routes, total_travel_seconds, total_walk_metres

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_ingest_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_ingest_key(key: str | None = Security(_ingest_key_header)) -> None:
    """
    Optional API-key guard for the ingest endpoint.

    If INGEST_API_KEY is not set the endpoint is open (local dev / testing).
    If it is set, the request must include the matching X-API-Key header.
    """
    if not INGEST_API_KEY:
        return  # no key configured → open
    if key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")

scheduler = AsyncIOScheduler()

# ---------------------------------------------------------------------------
# Route cache — keyed by (origin, destination, YYYY-MM-DD, HH:MM)
# Caches raw find_routes() output (legs only); risk scoring is always fresh.
# ---------------------------------------------------------------------------
_routes_cache: dict[tuple[str, str, str, str], tuple[list, datetime]] = {}
_ROUTES_CACHE_TTL = timedelta(hours=1)


def _routes_cache_key(origin: str, destination: str, departure_dt: datetime) -> tuple[str, str, str, str]:
    """Stable cache key at minute resolution."""
    return (origin, destination, departure_dt.strftime("%Y-%m-%d"), departure_dt.strftime("%H:%M"))


def _get_cached_routes(key: tuple[str, str, str, str]) -> list | None:
    entry = _routes_cache.get(key)
    if entry is None:
        return None
    cached_routes, cached_at = entry
    if datetime.now() - cached_at > _ROUTES_CACHE_TTL:
        del _routes_cache[key]
        return None
    return cached_routes


def _store_cached_routes(key: tuple[str, str, str, str], routes: list) -> None:
    _routes_cache[key] = (routes, datetime.now())


def _clear_routes_cache() -> None:
    _routes_cache.clear()
    logger.info("Route cache cleared.")


async def _daily_gtfs_refresh() -> None:
    """
    Scheduled job: refresh GTFS static data, rebuild the graph, and
    reseed reliability records.

    Runs every GTFS_REFRESH_HOURS hours (default 24).  Opens its own DB
    session because APScheduler jobs run outside FastAPI's DI system.
    Exceptions are caught and logged so a transient network failure cannot
    crash the scheduler process.

    Uses fill_gaps_only=True to preserve accumulated RT observations; new
    routes/stops that have no records yet still get synthetic priors.
    """
    logger.info("Daily GTFS static refresh starting.")
    db = SessionLocal()
    try:
        await refresh_static_data(db)
        build_graph(db)
        seeded = seed_from_static(db, fill_gaps_only=True)
        logger.info("Daily GTFS static refresh complete: %d reliability records reseeded.", seeded)
        _clear_routes_cache()
    except Exception as exc:
        logger.error("Daily GTFS static refresh failed: %s", exc, exc_info=True)
    finally:
        db.close()


async def _rt_poll_and_observe() -> None:
    """Poll all GTFS-RT feeds then record observed departures into DB."""
    from ingestion.gtfs_realtime import observe_departures
    await poll_all()
    db = SessionLocal()
    try:
        count = observe_departures(db)
        if count:
            logger.info("RT observation: recorded %d departures.", count)
    except Exception as exc:
        logger.error("RT observation failed: %s", exc, exc_info=True)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    logger.info("Database initialised.")

    db = SessionLocal()
    try:
        build_graph(db)
    except Exception as exc:
        logger.warning("Could not build graph on startup (no GTFS data yet?): %s", exc)
    finally:
        db.close()

    # Daily GTFS static refresh — always registered regardless of RT key.
    scheduler.add_job(
        _daily_gtfs_refresh,
        "interval",
        hours=GTFS_REFRESH_HOURS,
        id="daily_gtfs_refresh",
    )

    if GTFS_RT_API_KEY:
        await _rt_poll_and_observe()
        logger.info("GTFS-RT initial poll complete.")
        if GTFS_RT_POLL_SECONDS > 0:
            scheduler.add_job(
                _rt_poll_and_observe,
                "interval",
                seconds=GTFS_RT_POLL_SECONDS,
                id="gtfs_rt_poll",
            )
            logger.info("GTFS-RT polling scheduled (every %ds).", GTFS_RT_POLL_SECONDS)
        else:
            logger.info("GTFS-RT periodic polling disabled (GTFS_RT_POLL_SECONDS=0) — startup fetch only.")
    else:
        logger.info("GTFS-RT polling disabled — GTFS_RT_API_KEY not set.")

    scheduler.start()
    logger.info(
        "Scheduler started. Daily GTFS refresh every %dh.", GTFS_REFRESH_HOURS
    )

    yield

    # Shutdown
    if scheduler.running:
        scheduler.shutdown()


app = FastAPI(
    title="GO Transit Reliability Router",
    description="Reliability-first routing for GO bus routes (Toronto ↔ Guelph).",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
async def health(session: Session = Depends(get_session)) -> HealthResponse:
    """
    Liveness + data-freshness check.

    Returns DB record counts, graph stats, and timestamps so operators can
    quickly tell whether GTFS data has been loaded and the graph is ready.
    """
    from db.models import ReliabilityRecord, Stop, Trip
    from sqlalchemy import func, text as sa_text

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
        "timestamp": datetime.utcnow().isoformat(),
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
        },
        "gtfs_rt": {
            "polling_active": GTFS_RT_API_KEY != "" and GTFS_RT_POLL_SECONDS > 0 and scheduler.running,
            "startup_fetch_only": GTFS_RT_API_KEY != "" and GTFS_RT_POLL_SECONDS == 0,
        },
    }


@app.get("/stops", response_model=list[StopResult])
async def search_stops(
    query: str = Query(..., min_length=2, description="Stop name substring to search"),
    session: Session = Depends(get_session),
) -> list[StopResult]:
    """Search stops by name substring."""
    from collections import defaultdict
    from db.models import Stop, StopTime, Trip

    results = (
        session.query(Stop)
        .filter(Stop.stop_name.ilike(f"%{query}%"))
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


@app.get("/routes", response_model=RoutesResponse)
async def get_routes(
    origin: str = Query(..., description="Origin stop_id"),
    destination: str = Query(..., description="Destination stop_id"),
    departure_time: str | None = Query(
        None,
        description="Earliest departure time as HH:MM or HH:MM:SS. Defaults to current time.",
    ),
    travel_date: str | None = Query(
        None,
        description="Travel date as YYYY-MM-DD. Defaults to today.",
    ),
    explain: bool = Query(False, description="Include LLM plain-language explanation"),
    session: Session = Depends(get_session),
) -> RoutesResponse:
    """
    Return top-N scored routes from origin to destination.

    Routes have real scheduled departure/arrival times for the requested date
    and time.  Optionally include an LLM-generated explanation of tradeoffs.
    """
    # Parse departure datetime, defaulting to now.
    try:
        base_date = Date.fromisoformat(travel_date) if travel_date else datetime.now().date()
        if departure_time:
            parts = departure_time.split(":")
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
            departure_dt = datetime(base_date.year, base_date.month, base_date.day, h, m, s)
        else:
            now = datetime.now()
            departure_dt = datetime(base_date.year, base_date.month, base_date.day,
                                    now.hour, now.minute, now.second)
    except (ValueError, IndexError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date/time parameter: {exc}")

    cache_key = _routes_cache_key(origin, destination, departure_dt)
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
        if routes:
            _store_cached_routes(cache_key, routes)

    if not routes:
        raise HTTPException(status_code=404, detail="No routes found between these stops.")

    query_dt = datetime.now()
    time_bucket = classify_time_bucket(query_dt)

    scored_routes: list[dict[str, Any]] = []
    for route_legs in routes:
        scored_legs = []
        route_risk_scores = []

        for leg in route_legs:
            if leg["kind"] != "trip":
                scored_legs.append({**leg, "risk": None})
                continue

            hist = get_historical_reliability(
                leg["route_id"], leg["from_stop_id"], time_bucket, session
            )
            live = compute_live_risk(
                route_id=leg["route_id"],
                stop_id=leg["from_stop_id"],
                trip_id=leg["trip_id"],
                departure_time_str=leg["departure_time"],
                query_dt=query_dt,
                historical_reliability=hist,
            )
            scored_legs.append({**leg, "risk": live})
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

    response: dict[str, Any] = {"routes": scored_routes}

    if explain:
        from ingestion.gtfs_realtime import service_alerts
        alerts_payload = [
            {"header": a.header, "description": a.description,
             "routes": a.affected_route_ids, "stops": a.affected_stop_ids}
            for a in service_alerts
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


@app.post("/ingest/gtfs-static", response_model=IngestResponse)
async def trigger_gtfs_ingest(
    session: Session = Depends(get_session),
    _: None = Depends(_require_ingest_key),
) -> IngestResponse:
    """
    Manually trigger a GTFS static data refresh, graph rebuild, and
    reliability reseed.  (In production this runs on a daily schedule.)

    The reseed always runs as a full overwrite (fill_gaps_only=False) so
    that synthetic priors stay in sync with the updated schedule.  Once
    GTFS-RT data is flowing, the daily scheduler should switch to
    fill_gaps_only=True to preserve accumulated real observations.
    """
    await refresh_static_data(session)
    build_graph(session)
    seeded = seed_from_static(session, fill_gaps_only=False)
    _clear_routes_cache()
    return {
        "status": "ok",
        "message": (
            f"GTFS static data refreshed, graph rebuilt, "
            f"and {seeded} reliability records reseeded."
        ),
    }


@app.post("/ingest/reliability-seed", response_model=SeedResponse)
async def trigger_reliability_seed(
    window_days: int = Query(14, ge=1, le=90, description="Days of schedule to sample"),
    session: Session = Depends(get_session),
    _: None = Depends(_require_ingest_key),
) -> SeedResponse:
    """
    Seed reliability_records from the static GTFS schedule.

    Uses synthetic per-bucket reliability priors (no GTFS-RT required).
    Safe to call repeatedly — existing records are overwritten.
    Run this once after /ingest/gtfs-static to populate baseline risk scores.
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
