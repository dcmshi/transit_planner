"""
Live risk modifiers applied on top of historical reliability.

Conceptual model (from CLAUDE.md):
  P(miss_now) = f(historical_risk, live_conditions)

Live signals (each adjusts score, none is treated as definitive):
  - Active service alerts for this route/stop
  - Same-day cancellations on earlier trips of the same route
  - Missing vehicle position near scheduled departure time
  - Late-evening or weekend service

Score is a 0–1 risk value (higher = riskier).
"""

import logging
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any

from gtfs_time import hms_to_seconds as _hms_to_seconds
from ingestion.gtfs_realtime import (
    ServiceAlertState,
    service_alerts,
    trip_updates,
    vehicle_positions,
)

logger = logging.getLogger(__name__)

# Risk adjustment constants (tunable)
ALERT_RISK_BUMP = 0.10
CANCELLATION_RISK_BUMP = 0.15
MISSING_VEHICLE_RISK_BUMP = 0.08
LATE_EVENING_RISK_BUMP = 0.05
LATE_EVENING_START_SEC = 22 * 3600  # 22:00 — service thins out after this
WEEKEND_RISK_BUMP = 0.03
# Live running-late bumps (same-day trips only)
DELAY_MINOR_SECONDS = 5 * 60
DELAY_MAJOR_SECONDS = 15 * 60
DELAY_RISK_BUMP_MINOR = 0.05
DELAY_RISK_BUMP_MAJOR = 0.15


def get_live_delay(trip_id: str, stop_id: str) -> int | None:
    """
    Best-known live delay in seconds for a trip at a stop (positive = late,
    negative = early).  Prefers the stop's own RT override, falling back to
    the trip's overall delay.  Returns None when the trip is absent from the
    current trip-updates snapshot or is cancelled (a cancelled trip has no
    meaningful expected time).
    """
    tu = trip_updates.get(trip_id)
    if tu is None or tu.is_cancelled:
        return None
    return tu.stop_time_overrides.get(stop_id, tu.delay_seconds)


def compute_live_risk(
    route_id: str,
    stop_id: str,
    trip_id: str,
    departure_time_str: str,  # HH:MM:SS scheduled departure
    query_dt: datetime,
    historical_reliability: float,
    scheduled_dt: datetime | None = None,
    service_date: Date | None = None,
) -> dict[str, Any]:
    """
    Compute the final risk score for a single trip leg.

    Time-keyed modifiers (weekend, missing vehicle) are keyed off the leg's
    scheduled departure datetime, not the moment the query is made — a
    Friday query for Saturday travel must get the weekend bump, and a trip
    departing "in 10 minutes" tomorrow must not get the missing-vehicle
    bump.  Callers that know the travel date pass scheduled_dt (naive
    agency-local); when omitted it falls back to anchoring the GTFS time on
    query_dt's date (same-day semantics).

    Returns:
        {
          "risk_score":   float (0–1),
          "risk_label":   "Low" | "Medium" | "High",
          "modifiers":    list[str],  # human-readable modifier notes
          "is_cancelled": bool,
        }
    """
    # Normalise to naive agency-local wall clock for datetime arithmetic.
    query_naive = query_dt.replace(tzinfo=None) if query_dt.tzinfo else query_dt
    if scheduled_dt is None:
        scheduled_dt = datetime(
            query_naive.year, query_naive.month, query_naive.day
        ) + timedelta(seconds=_hms_to_seconds(departure_time_str))
    elif scheduled_dt.tzinfo:
        scheduled_dt = scheduled_dt.replace(tzinfo=None)

    # Start from the inverse of historical reliability
    base_risk = 1.0 - historical_reliability
    total_adjustment = 0.0
    modifiers: list[str] = []

    # GTFS-RT snapshots describe *today's* runs — per-trip live signals
    # (cancellation, running late, same-day cancellations) must not leak
    # onto future-dated queries.  Gate on the SERVICE day when the caller
    # knows it: a >24:00:00 leg rolls scheduled_dt onto tomorrow's date
    # even though the bus belongs to (and is live in) today's service.
    is_same_day = (service_date or scheduled_dt.date()) == query_naive.date()

    # 1. Is this trip currently cancelled?
    tu = trip_updates.get(trip_id)
    if tu and tu.is_cancelled and is_same_day:
        return {
            "risk_score": 1.0,
            "risk_label": "High",
            "modifiers": ["Trip is currently marked as cancelled in GTFS-RT."],
            "is_cancelled": True,
        }

    # 2. Active service alerts touching this route or stop
    active_alerts = _alerts_for(route_id, stop_id)
    if active_alerts:
        total_adjustment += ALERT_RISK_BUMP * len(active_alerts)
        for a in active_alerts:
            modifiers.append(f"Service alert: {a.header}")

    # 3. Earlier same-day cancellations on this route
    if is_same_day:
        same_route_cancels = _same_route_cancellations(route_id)
        if same_route_cancels:
            total_adjustment += CANCELLATION_RISK_BUMP
            modifiers.append(
                f"{same_route_cancels} earlier cancellation(s) on route {route_id} today."
            )

    # 3b. Trip already running late right now
    if is_same_day:
        delay = get_live_delay(trip_id, stop_id)
        if delay is not None and delay >= DELAY_MAJOR_SECONDS:
            total_adjustment += DELAY_RISK_BUMP_MAJOR
            modifiers.append(f"Trip is currently running ~{round(delay / 60)} min late.")
        elif delay is not None and delay >= DELAY_MINOR_SECONDS:
            total_adjustment += DELAY_RISK_BUMP_MINOR
            modifiers.append(f"Trip is currently running ~{round(delay / 60)} min late.")

    # 4. Vehicle position missing near departure — full-datetime comparison
    #    so a "10 minutes past the hour" departure on a future date does not
    #    trigger the window.
    minutes_until_departure = (scheduled_dt - query_naive).total_seconds() / 60

    if 0 < minutes_until_departure <= 15 and trip_id not in vehicle_positions:
        total_adjustment += MISSING_VEHICLE_RISK_BUMP
        modifiers.append("No vehicle position data found close to departure.")

    # 5. Late-evening service — from the GTFS time string so >24:00:00
    #    post-midnight departures still count as late evening.
    dep_seconds = _hms_to_seconds(departure_time_str)
    if dep_seconds >= LATE_EVENING_START_SEC:
        total_adjustment += LATE_EVENING_RISK_BUMP
        modifiers.append("Late-evening departure (after 22:00) — reduced service frequency.")

    # 6. Weekend service — keyed to the travel day, not the query day.
    if scheduled_dt.weekday() >= 5:
        total_adjustment += WEEKEND_RISK_BUMP
        modifiers.append("Weekend service — less frequent, higher no-show rate historically.")

    final_risk = min(1.0, base_risk + total_adjustment)
    return {
        "risk_score": round(final_risk, 3),
        "risk_label": _risk_label(final_risk),
        "modifiers": modifiers,
        "is_cancelled": False,
    }


def _alerts_for(route_id: str, stop_id: str) -> list[ServiceAlertState]:
    # list(...) snapshot: this runs in request worker threads while the
    # poller clears/extends the shared list on the event loop — iterating
    # the live object can raise "changed size during iteration".
    return [
        a for a in list(service_alerts)
        if route_id in a.affected_route_ids or stop_id in a.affected_stop_ids
    ]


def _same_route_cancellations(route_id: str) -> int:
    # list(...) snapshot — see _alerts_for.
    return sum(
        1 for tu in list(trip_updates.values())
        if tu.route_id == route_id and tu.is_cancelled
    )


def _risk_label(score: float) -> str:
    if score < 0.33:
        return "Low"
    if score < 0.66:
        return "Medium"
    return "High"


