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
from datetime import datetime, timedelta
from typing import Any

from ingestion.gtfs_realtime import (
    ServiceAlertState,
    TripUpdateState,
    service_alerts,
    trip_updates,
    vehicle_positions,
)

logger = logging.getLogger(__name__)

# Risk adjustment constants (tunable)
ALERT_RISK_BUMP = 0.10
CANCELLATION_RISK_BUMP = 0.15
MISSING_VEHICLE_RISK_BUMP = 0.08
LATE_EVENING_RISK_BUMP = 0.05   # after 22:00
WEEKEND_RISK_BUMP = 0.03


def compute_live_risk(
    route_id: str,
    stop_id: str,
    trip_id: str,
    departure_time_str: str,  # HH:MM:SS scheduled departure
    query_dt: datetime,
    historical_reliability: float,
) -> dict[str, Any]:
    """
    Compute the final risk score for a single trip leg.

    Returns:
        {
          "risk_score":   float (0–1),
          "risk_label":   "Low" | "Medium" | "High",
          "modifiers":    list[str],  # human-readable modifier notes
          "is_cancelled": bool,
        }
    """
    # Start from the inverse of historical reliability
    base_risk = 1.0 - historical_reliability
    total_adjustment = 0.0
    modifiers: list[str] = []

    # 1. Is this trip currently cancelled?
    tu = trip_updates.get(trip_id)
    if tu and tu.is_cancelled:
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
    same_route_cancels = _same_route_cancellations(route_id)
    if same_route_cancels:
        total_adjustment += CANCELLATION_RISK_BUMP
        modifiers.append(
            f"{same_route_cancels} earlier cancellation(s) on route {route_id} today."
        )

    # 4. Vehicle position missing near departure
    dep_seconds = _hms_to_seconds(departure_time_str)
    query_seconds = query_dt.hour * 3600 + query_dt.minute * 60 + query_dt.second
    minutes_until_departure = (dep_seconds - query_seconds) / 60

    if 0 < minutes_until_departure <= 15 and trip_id not in vehicle_positions:
        total_adjustment += MISSING_VEHICLE_RISK_BUMP
        modifiers.append("No vehicle position data found close to departure.")

    # 5. Late-evening service
    if dep_seconds >= 22 * 3600:
        total_adjustment += LATE_EVENING_RISK_BUMP
        modifiers.append("Late-evening departure (after 22:00) — reduced service frequency.")

    # 6. Weekend service
    if query_dt.weekday() >= 5:
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
    return [
        a for a in service_alerts
        if route_id in a.affected_route_ids or stop_id in a.affected_stop_ids
    ]


def _same_route_cancellations(route_id: str) -> int:
    return sum(
        1 for tu in trip_updates.values()
        if tu.route_id == route_id and tu.is_cancelled
    )


def _risk_label(score: float) -> str:
    if score < 0.33:
        return "Low"
    if score < 0.66:
        return "Medium"
    return "High"


def _hms_to_seconds(hms: str) -> int:
    try:
        h, m, s = hms.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return 0
