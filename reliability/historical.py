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
from datetime import datetime, time

from sqlalchemy.orm import Session

from db.models import ReliabilityRecord

logger = logging.getLogger(__name__)

# Rolling window length in days
WINDOW_DAYS = 14

# Time bucket definitions (start_hour inclusive, end_hour exclusive, weekday-only)
_BUCKETS = [
    ("weekday_am_peak", 6, 9),
    ("weekday_pm_peak", 15, 19),
]


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
) -> None:
    """
    Record one observed (or cancelled) departure and update reliability stats.
    TODO: Call this from a background job that processes historical GTFS-RT data.
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
        )
        session.add(record)

    record.scheduled_departures += 1
    if was_cancelled:
        record.cancellation_count += 1
    else:
        record.observed_departures += 1
        record.total_delay_seconds += delay_seconds

    record.window_end_date = date_str
    record.updated_at = datetime.utcnow().isoformat()
    session.commit()
