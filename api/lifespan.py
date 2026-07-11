"""
Application lifecycle: startup/shutdown, the APScheduler jobs (daily GTFS
static refresh, GTFS-RT polling), and the shared ingest slot.

On startup:
  1. Initialise the database schema.
  2. Build the transit graph from stored GTFS data (if available).
  3. Start the APScheduler:
       - Daily GTFS static refresh + graph rebuild + reliability reseed
         (every GTFS_REFRESH_HOURS, default 24h — always active).
       - GTFS-RT polling every GTFS_RT_POLL_SECONDS
         (only when GTFS_RT_API_KEY is set).
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from api.cache import _clear_routes_cache
from config import (
    GTFS_REFRESH_HOURS,
    GTFS_RT_ALERTS_URL,
    GTFS_RT_API_KEY,
    GTFS_RT_POLL_SECONDS,
    GTFS_RT_TRIP_UPDATES_URL,
    GTFS_RT_VEHICLE_POSITIONS_URL,
    GTFS_STATIC_URL,
)
from db.session import SessionLocal, init_db
from graph.builder import build_graph
from ingestion.gtfs_realtime import poll_all
from ingestion.gtfs_static import refresh_static_data
from ingestion.seed_reliability import seed_from_static
from reliability.historical import decay_reliability_records

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# ---------------------------------------------------------------------------
# Ingest job state — the manual endpoint and the daily scheduled refresh
# share one slot so two full ingests can never run concurrently.  All
# writers run on the event loop, so plain check-and-set is race-free.
# ---------------------------------------------------------------------------
_ingest_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_status": None,   # "ok" | "error" | None (never run)
    "last_message": None,
}


def _try_begin_ingest() -> bool:
    """Claim the single ingest slot; False if an ingest is already running."""
    if _ingest_state["running"]:
        return False
    _ingest_state.update(
        running=True,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
    )
    return True


def _finish_ingest(status: str, message: str) -> None:
    _ingest_state.update(
        running=False,
        finished_at=datetime.now(timezone.utc).isoformat(),
        last_status=status,
        last_message=message,
    )


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
    if not _try_begin_ingest():
        logger.info("Daily GTFS refresh skipped — an ingest is already running.")
        return
    logger.info("Daily GTFS static refresh starting.")
    db = SessionLocal()
    try:
        await refresh_static_data(db)
        # build_graph and seed_from_static are CPU/DB-bound sync calls; run
        # them in a worker thread so the event loop keeps serving requests.
        await asyncio.to_thread(build_graph, db)
        # Age out old reliability counters (half-life WINDOW_DAYS) before
        # reseeding fills any gaps at full synthetic strength.
        await asyncio.to_thread(decay_reliability_records, db)
        seeded = await asyncio.to_thread(seed_from_static, db, fill_gaps_only=True)
        logger.info("Daily GTFS static refresh complete: %d reliability records reseeded.", seeded)
        _clear_routes_cache()
        _finish_ingest("ok", f"Daily refresh: {seeded} reliability records reseeded.")
    except Exception as exc:
        logger.error("Daily GTFS static refresh failed: %s", exc, exc_info=True)
        _finish_ingest("error", str(exc))
    finally:
        db.close()
        # Same CancelledError guard as _run_gtfs_ingest — never leave the
        # shared ingest slot claimed.
        if _ingest_state["running"]:
            _finish_ingest("error", "Daily refresh cancelled before completion.")


async def _run_gtfs_ingest() -> None:
    """
    Background body of POST /ingest/gtfs-static: full static refresh, graph
    rebuild, and full reseed (fill_gaps_only=False keeps synthetic priors in
    sync with the updated schedule; the daily scheduler uses True to
    preserve accumulated real observations).

    Opens its own session — the request's session closes when the 202
    returns.  The caller must have claimed the slot via _try_begin_ingest().
    """
    db = SessionLocal()
    try:
        await refresh_static_data(db)
        await asyncio.to_thread(build_graph, db)
        seeded = await asyncio.to_thread(seed_from_static, db, fill_gaps_only=False)
        _clear_routes_cache()
        msg = (
            f"GTFS static data refreshed, graph rebuilt, "
            f"and {seeded} reliability records reseeded."
        )
        logger.info("Manual ingest complete: %s", msg)
        _finish_ingest("ok", msg)
    except Exception as exc:
        logger.error("Manual ingest failed: %s", exc, exc_info=True)
        _finish_ingest("error", str(exc))
    finally:
        db.close()
        # CancelledError is a BaseException and bypasses the handler above
        # (e.g. shutdown cancels pending tasks) — never leave the slot
        # claimed, or every future ingest AND daily refresh is blocked.
        if _ingest_state["running"]:
            _finish_ingest("error", "Ingest task cancelled before completion.")


# Strong reference to the running ingest task — asyncio keeps only weak
# refs to tasks, so a discarded reference could be garbage-collected
# mid-run (documented asyncio requirement).
_ingest_task: "asyncio.Task | None" = None


def start_ingest_task() -> None:
    """Launch the manual-ingest background task, holding the strong module
    reference.  The caller must have claimed the slot via _try_begin_ingest()."""
    global _ingest_task
    _ingest_task = asyncio.create_task(_run_gtfs_ingest())


async def _rt_poll_and_observe() -> None:
    """Poll all GTFS-RT feeds then record observed departures and no-shows."""
    from ingestion.gtfs_realtime import observe_departures, record_no_shows
    await poll_all()
    db = SessionLocal()
    try:
        count = await asyncio.to_thread(observe_departures, db)
        if count:
            logger.info("RT observation: recorded %d departures.", count)
        # After observed/cancelled trips are recorded (and the service-day
        # rollover has run), sweep for trips that never showed up at all.
        missed = await asyncio.to_thread(record_no_shows, db)
        if missed:
            logger.info("RT observation: recorded %d no-show departures.", missed)
    except Exception as exc:
        logger.error("RT observation failed: %s", exc, exc_info=True)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — validate required config early so problems surface immediately.
    if not GTFS_STATIC_URL:
        logger.warning(
            "GTFS_STATIC_URL is not set. POST /ingest/gtfs-static will fail. "
            "Set it in .env to a Metrolinx GTFS ZIP URL before ingesting."
        )
    if GTFS_RT_API_KEY and not all([GTFS_RT_TRIP_UPDATES_URL, GTFS_RT_VEHICLE_POSITIONS_URL, GTFS_RT_ALERTS_URL]):
        logger.warning(
            "GTFS_RT_API_KEY is set but one or more RT feed URLs are missing "
            "(GTFS_RT_TRIP_UPDATES_URL, GTFS_RT_VEHICLE_POSITIONS_URL, GTFS_RT_ALERTS_URL). "
            "RT polling will be skipped for unconfigured feeds."
        )

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
