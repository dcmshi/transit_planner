"""
Historical reliability tracking.

Maintains rolling-window (14–30 day) statistics per:
  route_id × stop_id × time_bucket

Time buckets:
  weekday_am_peak   06:00–09:00
  weekday_pm_peak   15:00–19:00
  weekday_offpeak   all other weekday hours
  weekend           Saturday + Sunday

Reliability score (0–1, higher = more reliable) is derived from:
  observed_rate  = observed_departures / scheduled_departures
  avg_delay_min  = total_delay_seconds / observed_departures / 60
  cancel_rate    = cancellation_count / scheduled_departures
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import AGENCY_TZ
from db.models import ReliabilityRecord

logger = logging.getLogger(__name__)

# Rolling-window half-life in days: counters decay by 50% over this span
# (see decay_reliability_records), so stats always reflect roughly the last
# couple of window lengths rather than accumulating forever.
WINDOW_DAYS = 14


def classify_time_bucket(dt: datetime) -> str:
    """Return the time bucket label for a given datetime."""
    if dt.weekday() >= 5:
        return "weekend"
    hour = dt.hour
    if 6 <= hour < 9:
        return "weekday_am_peak"
    if 15 <= hour < 19:
        return "weekday_pm_peak"
    return "weekday_offpeak"


def get_historical_reliability(
    route_id: str,
    stop_id: str,
    time_bucket: str,
    session: Session,
) -> float:
    """
    Return a 0–1 reliability score for a given route/stop/time_bucket.
    Returns 0.8 (neutral prior) if no data is available.
    """
    record = (
        session.query(ReliabilityRecord)
        .filter_by(route_id=route_id, stop_id=stop_id, time_bucket=time_bucket)
        .order_by(ReliabilityRecord.updated_at.desc())
        .first()
    )
    if record is None or record.scheduled_departures == 0:
        logger.debug(
            "No historical data for route=%s stop=%s bucket=%s; using neutral prior.",
            route_id, stop_id, time_bucket,
        )
        return 0.8  # neutral prior

    observed_rate = record.observed_departures / record.scheduled_departures
    cancel_rate = record.cancellation_count / record.scheduled_departures
    avg_delay_min = (
        record.total_delay_seconds / record.observed_departures / 60
        if record.observed_departures > 0 else 0
    )

    # Simple weighted combination — tunable
    delay_penalty = min(avg_delay_min / 30, 1.0) * 0.2  # up to 0.2 penalty at 30-min avg delay
    score = observed_rate * (1 - cancel_rate) - delay_penalty
    return max(0.0, min(1.0, score))


def record_observed_departure(
    route_id: str,
    stop_id: str,
    scheduled_at: datetime,
    delay_seconds: int,
    was_cancelled: bool,
    session: Session,
    was_missed: bool = False,
) -> None:
    """
    Record one observed, cancelled, or missed departure and update
    reliability stats.  Called by ingestion.gtfs_realtime after every RT
    poll (observe_departures for observed/cancelled, record_no_shows for
    missed).

    was_missed=True records a scheduled departure with no RT evidence at
    all — scheduled_departures increments but observed_departures does not,
    which is what drives observed_rate down for no-shows.

    Does not commit — the caller owns the transaction and commits once per
    batch.  New records are flushed so subsequent lookups in the same batch
    see them (autoflush is off).
    """
    bucket = classify_time_bucket(scheduled_at)
    date_str = scheduled_at.strftime("%Y%m%d")

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
            window_start_date=date_str,
            scheduled_departures=0,
            observed_departures=0,
            total_delay_seconds=0,
            cancellation_count=0,
            source="observed",
        )
        session.add(record)
        session.flush()
    elif record.source == "seed":
        record.source = "mixed"  # synthetic prior now blended with real data

    record.scheduled_departures += 1
    if was_cancelled:
        record.cancellation_count += 1
    elif was_missed:
        pass  # no-show: scheduled but never seen — observed_rate drops
    else:
        record.observed_departures += 1
        record.total_delay_seconds += delay_seconds

    record.window_end_date = date_str
    record.updated_at = datetime.now(timezone.utc).isoformat()


# Agency-local date of the last decay run — guards against the daily job
# firing more than once per day (e.g. GTFS_REFRESH_HOURS < 24).  In-memory
# only: a same-day restart plus refresh could decay twice, which slightly
# shortens the effective half-life for that one day — harmless.
_last_decay_date: str = ""


def decay_reliability_records(session: Session, days_elapsed: float = 1.0) -> int:
    """
    Apply exponential age-decay to all reliability counters.

    Called once per day by the daily GTFS refresh job.  Counters halve over
    WINDOW_DAYS days, so a bad month fades instead of permanently
    depressing a route's score.  All four counters are scaled by the same
    factor, which preserves observed_rate / cancel_rate / avg-delay exactly —
    decay changes nothing by itself; it makes *new* observations weigh more
    against the shrunken denominator.

    Returns the number of rows updated (0 when skipped as already run today).
    """
    global _last_decay_date

    today = datetime.now(AGENCY_TZ).strftime("%Y%m%d")
    if today == _last_decay_date:
        return 0

    factor = 0.5 ** (days_elapsed / WINDOW_DAYS)
    result = session.execute(
        text("""
            UPDATE reliability_records SET
                scheduled_departures = CAST(ROUND(scheduled_departures * :f) AS INT),
                observed_departures  = CAST(ROUND(observed_departures  * :f) AS INT),
                total_delay_seconds  = CAST(ROUND(total_delay_seconds  * :f) AS INT),
                cancellation_count   = CAST(ROUND(cancellation_count   * :f) AS INT)
        """),
        {"f": factor},
    )
    session.commit()
    _last_decay_date = today
    logger.info(
        "Reliability decay applied: %d records scaled by %.4f (half-life %d days).",
        result.rowcount, factor, WINDOW_DAYS,
    )
    return result.rowcount
