"""
FastAPI application entry point.

On startup:
  1. Initialise the database schema.
  2. Build the transit graph from stored GTFS data (if available).
  3. Start the GTFS-RT polling scheduler.

Endpoints (v1):
  GET /routes?origin=<stop_id>&destination=<stop_id>&explain=<bool>
  GET /stops?query=<name>
  GET /health
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from config import GTFS_RT_API_KEY, GTFS_RT_POLL_SECONDS, MAX_ROUTES
from db.session import SessionLocal, get_session, init_db
from graph.builder import build_graph, get_graph
from ingestion.gtfs_realtime import poll_all
from ingestion.gtfs_static import refresh_static_data
from llm.explainer import explain_routes
from reliability.historical import classify_time_bucket, get_historical_reliability
from reliability.live import compute_live_risk
from routing.engine import find_routes, total_travel_seconds

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


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

    if GTFS_RT_API_KEY:
        scheduler.add_job(poll_all, "interval", seconds=GTFS_RT_POLL_SECONDS)
        scheduler.start()
        logger.info("GTFS-RT polling started (every %ds).", GTFS_RT_POLL_SECONDS)
    else:
        logger.info("GTFS-RT polling disabled — GTFS_RT_API_KEY not set.")

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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/stops")
async def search_stops(
    query: str = Query(..., min_length=2, description="Stop name substring to search"),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Search stops by name substring."""
    from db.models import Stop
    results = (
        session.query(Stop)
        .filter(Stop.stop_name.ilike(f"%{query}%"))
        .limit(20)
        .all()
    )
    return [
        {"stop_id": s.stop_id, "stop_name": s.stop_name, "lat": s.stop_lat, "lon": s.stop_lon}
        for s in results
    ]


@app.get("/routes")
async def get_routes(
    origin: str = Query(..., description="Origin stop_id"),
    destination: str = Query(..., description="Destination stop_id"),
    explain: bool = Query(False, description="Include LLM plain-language explanation"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """
    Return top-N scored routes from origin to destination.
    Optionally include an LLM-generated explanation of tradeoffs.
    """
    try:
        routes = find_routes(origin, destination, max_routes=MAX_ROUTES)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Routing error: {exc}")

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


@app.post("/ingest/gtfs-static")
async def trigger_gtfs_ingest(session: Session = Depends(get_session)) -> dict[str, str]:
    """
    Manually trigger a GTFS static data refresh and graph rebuild.
    (In production this runs on a daily schedule.)
    """
    await refresh_static_data(session)
    build_graph(session)
    return {"status": "ok", "message": "GTFS static data refreshed and graph rebuilt."}
