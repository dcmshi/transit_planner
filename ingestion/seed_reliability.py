"""
Seed the reliability_records table from GTFS static schedule data.

Without a GTFS-RT history, we cannot know actual vs scheduled performance.
Instead this module:
  1. Counts scheduled departures per (route_id, stop_id, time_bucket)
     across a configurable window of dates drawn from the static feed.
  2. Writes synthetic ReliabilityRecord entries using per-bucket prior
     reliability rates based on known GO Transit operating patterns.

These priors give every route/stop/bucket a non-neutral starting score,
differentiated by time of day and day type.  Once real GTFS-RT data starts
flowing through record_observed_departure(), those calls will incrementally
replace the synthetic counts with real observations.

Default synthetic rates (tunable via _PRIORS):
  weekday_am_peak  — 85% observed, 3% cancelled, avg 3-min delay
  weekday_pm_peak  — 80% observed, 5% cancelled, avg 5-min delay  (worst)
  weekday_offpeak  — 90% observed, 2% cancelled, avg 2-min delay  (best)
  weekend          — 75% observed, 8% cancelled, avg 4-min delay
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from db.models import ReliabilityRecord
from reliability.historical import classify_time_bucket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Synthetic prior rates — replace with real data once GTFS-RT is available.
# Keys match the labels returned by classify_time_bucket().
# ---------------------------------------------------------------------------
_PRIORS: dict[str, dict] = {
    "weekday_am_peak": {
        "reliability_rate": 0.85,   # fraction of scheduled trips observed
        "cancellation_rate": 0.03,  # fraction outright cancelled
        "avg_delay_seconds": 180,   # average delay on observed trips
    },
    "weekday_pm_peak": {
        "reliability_rate": 0.80,
        "cancellation_rate": 0.05,
        "avg_delay_seconds": 300,
    },
    "weekday_offpeak": {
        "reliability_rate": 0.90,
        "cancellation_rate": 0.02,
        "avg_delay_seconds": 120,
    },
    "weekend": {
        "reliability_rate": 0.75,
        "cancellation_rate": 0.08,
        "avg_delay_seconds": 240,
    },
}


def seed_from_static(session: Session, window_days: int = 14) -> int:
    """
    Seed ReliabilityRecord from the static GTFS schedule.

    Finds a `window_days`-day slice of service dates in the DB (preferring
    dates near today), counts scheduled departures per
    (route_id, stop_id, time_bucket), then upserts ReliabilityRecord rows
    using the synthetic prior rates.  Existing records are overwritten so
    the function is safe to call repeatedly.

    Args:
        session:     SQLAlchemy session (DB must already have GTFS data).
        window_days: Number of calendar days to sample from the schedule.

    Returns:
        Number of ReliabilityRecord rows written.

    Raises:
        RuntimeError: If no trips are found in the database.
    """
    # --- Determine date window -----------------------------------------------
    row = session.execute(
        text("SELECT MIN(service_id), MAX(service_id) FROM trips")
    ).fetchone()

    if not row or not row[0]:
        raise RuntimeError("No trips in database — run POST /ingest/gtfs-static first.")

    try:
        db_min = datetime.strptime(row[0], "%Y%m%d").date()
        db_max = datetime.strptime(row[1], "%Y%m%d").date()
    except ValueError as exc:
        raise RuntimeError(
            f"service_id values are not YYYYMMDD dates: {exc}"
        ) from exc

    # Start from today if it falls within the feed; otherwise use the feed start.
    window_start = max(db_min, date.today()) if date.today() <= db_max else db_min
    window_end = min(window_start + timedelta(days=window_days - 1), db_max)

    start_str = window_start.strftime("%Y%m%d")
    end_str = window_end.strftime("%Y%m%d")

    logger.info(
        "Seeding reliability from GTFS static: window %s – %s (%d days).",
        start_str, end_str, (window_end - window_start).days + 1,
    )

    # --- Count scheduled departures per (route, stop, date, hour) -----------
    # dep_hour % 24 handles GTFS times past midnight (e.g. 25:10 → hour 1).
    rows = session.execute(
        text("""
            SELECT
                t.route_id,
                st.stop_id,
                t.service_id,
                CAST(substr(st.departure_time, 1, 2) AS INT) % 24 AS dep_hour,
                COUNT(*) AS trip_count
            FROM stop_times st
            JOIN trips t ON t.trip_id = st.trip_id
            WHERE t.service_id BETWEEN :start AND :end
            GROUP BY t.route_id, st.stop_id, t.service_id, dep_hour
        """),
        {"start": start_str, "end": end_str},
    ).fetchall()

    if not rows:
        logger.warning("No trips found in window %s – %s.", start_str, end_str)
        return 0

    logger.info("Aggregating %d (route, stop, date, hour) combinations.", len(rows))

    # --- Aggregate into (route_id, stop_id, time_bucket) ---------------------
    counts: dict[tuple[str, str, str], int] = defaultdict(int)

    for route_id, stop_id, service_id, dep_hour, trip_count in rows:
        try:
            service_dt = datetime.strptime(service_id, "%Y%m%d").replace(hour=dep_hour)
            bucket = classify_time_bucket(service_dt)
        except (ValueError, OverflowError):
            continue
        counts[(route_id, stop_id, bucket)] += trip_count

    # --- Upsert ReliabilityRecord rows ---------------------------------------
    written = 0
    for (route_id, stop_id, bucket), scheduled in counts.items():
        prior = _PRIORS[bucket]
        observed = round(scheduled * prior["reliability_rate"])
        cancelled = round(scheduled * prior["cancellation_rate"])
        total_delay = observed * prior["avg_delay_seconds"]

        record = (
            session.query(ReliabilityRecord)
            .filter_by(route_id=route_id, stop_id=stop_id, time_bucket=bucket)
            .first()
        )
        if record is None:
            record = ReliabilityRecord(
                route_id=route_id,
                stop_id=stop_id,
                time_bucket=bucket,
                window_start_date=start_str,
            )
            session.add(record)

        record.scheduled_departures = scheduled
        record.observed_departures = observed
        record.cancellation_count = cancelled
        record.total_delay_seconds = total_delay
        record.window_end_date = end_str
        record.updated_at = datetime.utcnow().isoformat()
        written += 1

    session.commit()
    logger.info("Reliability seed complete: %d records written.", written)
    return written
