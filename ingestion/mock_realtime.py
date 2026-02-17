"""
Mock GTFS-RT state injector for development and testing.

Directly populates the module-level state dicts in ingestion.gtfs_realtime
without making any network calls.  Use this to exercise the full live-risk
path (reliability/live.py) before a Metrolinx GTFS-RT API key is available.

WARNING: These functions mutate shared in-process state.  They are intended
for local development and automated tests only — do not call them in a
production environment with real user traffic.
"""

import logging
from datetime import datetime

from ingestion.gtfs_realtime import (
    ServiceAlertState,
    TripUpdateState,
    service_alerts,
    trip_updates,
    vehicle_positions,
)

logger = logging.getLogger(__name__)


def clear_all() -> None:
    """Reset all three live-state collections to empty."""
    trip_updates.clear()
    service_alerts.clear()
    vehicle_positions.clear()
    logger.debug("Mock RT state cleared.")


def inject_cancellation(trip_id: str, route_id: str) -> TripUpdateState:
    """
    Mark a trip as cancelled in the live state.

    Equivalent to receiving a GTFS-RT TripUpdate with
    schedule_relationship = CANCELED.
    """
    state = TripUpdateState(
        trip_id=trip_id,
        route_id=route_id,
        is_cancelled=True,
    )
    trip_updates[trip_id] = state
    logger.debug("Mock: injected cancellation for trip %s (route %s).", trip_id, route_id)
    return state


def inject_delay(
    trip_id: str,
    route_id: str,
    delay_seconds: int,
    stop_overrides: dict[str, int] | None = None,
) -> TripUpdateState:
    """
    Mark a trip as delayed by delay_seconds in the live state.

    Args:
        trip_id:        GTFS trip_id.
        route_id:       GTFS route_id.
        delay_seconds:  Positive = late, negative = early (matches GTFS-RT sign).
        stop_overrides: Per-stop delay overrides {stop_id: delay_seconds}.
    """
    state = TripUpdateState(
        trip_id=trip_id,
        route_id=route_id,
        delay_seconds=delay_seconds,
        stop_time_overrides=stop_overrides or {},
    )
    trip_updates[trip_id] = state
    logger.debug(
        "Mock: injected delay of %ds for trip %s (route %s).",
        delay_seconds, trip_id, route_id,
    )
    return state


def inject_alert(
    alert_id: str,
    header: str,
    description: str = "",
    route_ids: list[str] | None = None,
    stop_ids: list[str] | None = None,
) -> ServiceAlertState:
    """
    Add a service alert to the live state.

    Args:
        alert_id:    Unique identifier for the alert.
        header:      Short human-readable title.
        description: Longer description (optional).
        route_ids:   GTFS route_ids affected by this alert.
        stop_ids:    GTFS stop_ids affected by this alert.
    """
    state = ServiceAlertState(
        alert_id=alert_id,
        header=header,
        description=description,
        affected_route_ids=route_ids or [],
        affected_stop_ids=stop_ids or [],
    )
    service_alerts.append(state)
    logger.debug(
        "Mock: injected alert %s affecting routes=%s stops=%s.",
        alert_id, route_ids, stop_ids,
    )
    return state


def inject_vehicle_position(
    trip_id: str,
    lat: float,
    lon: float,
    timestamp: int | None = None,
) -> None:
    """
    Set a vehicle position for a trip in the live state.

    Args:
        trip_id:   GTFS trip_id.
        lat:       Latitude (decimal degrees).
        lon:       Longitude (decimal degrees).
        timestamp: Unix epoch seconds.  Defaults to now if omitted.
    """
    vehicle_positions[trip_id] = {
        "lat": lat,
        "lon": lon,
        "timestamp": timestamp if timestamp is not None else int(datetime.utcnow().timestamp()),
    }
    logger.debug("Mock: injected vehicle position for trip %s at (%.4f, %.4f).", trip_id, lat, lon)


def get_state_summary() -> dict:
    """
    Return a snapshot of the current live RT state.

    Useful for debugging and for verifying injected state via the API.
    """
    return {
        "trip_updates": {
            tid: {
                "route_id": tu.route_id,
                "delay_seconds": tu.delay_seconds,
                "is_cancelled": tu.is_cancelled,
            }
            for tid, tu in trip_updates.items()
        },
        "service_alerts": [
            {
                "alert_id": a.alert_id,
                "header": a.header,
                "affected_route_ids": a.affected_route_ids,
                "affected_stop_ids": a.affected_stop_ids,
            }
            for a in service_alerts
        ],
        "vehicle_positions": {
            tid: {"lat": pos["lat"], "lon": pos["lon"]}
            for tid, pos in vehicle_positions.items()
        },
    }
