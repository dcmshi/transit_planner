"""
Unit tests for the reliability modules.

reliability.historical — pure functions only (classify_time_bucket).
reliability.live       — compute_live_risk, which reads module-level
                         GTFS-RT state; patched via unittest.mock.
"""

import pytest
from datetime import datetime
from unittest.mock import patch

from reliability.historical import classify_time_bucket
from reliability.live import (
    ALERT_RISK_BUMP,
    CANCELLATION_RISK_BUMP,
    LATE_EVENING_RISK_BUMP,
    MISSING_VEHICLE_RISK_BUMP,
    WEEKEND_RISK_BUMP,
    compute_live_risk,
)
from ingestion.gtfs_realtime import ServiceAlertState, TripUpdateState


# ---------------------------------------------------------------------------
# classify_time_bucket
# ---------------------------------------------------------------------------

class TestClassifyTimeBucket:
    # Weekday = Monday (weekday() == 0) = 2026-02-09

    def test_am_peak_start(self):
        assert classify_time_bucket(datetime(2026, 2, 9, 6, 0)) == "weekday_am_peak"

    def test_am_peak_middle(self):
        assert classify_time_bucket(datetime(2026, 2, 9, 7, 30)) == "weekday_am_peak"

    def test_am_peak_end_exclusive(self):
        # 09:00 is NOT am_peak — ends at < 9
        assert classify_time_bucket(datetime(2026, 2, 9, 9, 0)) == "weekday_offpeak"

    def test_pm_peak_start(self):
        assert classify_time_bucket(datetime(2026, 2, 9, 15, 0)) == "weekday_pm_peak"

    def test_pm_peak_middle(self):
        assert classify_time_bucket(datetime(2026, 2, 9, 17, 0)) == "weekday_pm_peak"

    def test_pm_peak_end_exclusive(self):
        # 19:00 is NOT pm_peak — ends at < 19
        assert classify_time_bucket(datetime(2026, 2, 9, 19, 0)) == "weekday_offpeak"

    def test_midday_offpeak(self):
        assert classify_time_bucket(datetime(2026, 2, 9, 12, 0)) == "weekday_offpeak"

    def test_early_morning_offpeak(self):
        assert classify_time_bucket(datetime(2026, 2, 9, 5, 59)) == "weekday_offpeak"

    def test_late_evening_offpeak(self):
        assert classify_time_bucket(datetime(2026, 2, 9, 22, 0)) == "weekday_offpeak"

    def test_saturday(self):
        # 2026-02-07 is a Saturday
        assert classify_time_bucket(datetime(2026, 2, 7, 8, 0)) == "weekend"

    def test_sunday(self):
        # 2026-02-08 is a Sunday
        assert classify_time_bucket(datetime(2026, 2, 8, 15, 30)) == "weekend"

    def test_friday_is_weekday(self):
        # 2026-02-13 is a Friday
        assert classify_time_bucket(datetime(2026, 2, 13, 8, 0)) == "weekday_am_peak"


# ---------------------------------------------------------------------------
# compute_live_risk
# Patches are applied to reliability.live's own namespace, since the
# module-level dicts are imported by name at import time.
# ---------------------------------------------------------------------------

_LIVE = "reliability.live"


def _compute(departure="14:00:00", query_dt=None, hist=0.8, route="R1", stop="S1", trip="T1"):
    """Helper to call compute_live_risk with sensible defaults."""
    if query_dt is None:
        query_dt = datetime(2026, 2, 9, 13, 0)  # weekday, well before departure
    return compute_live_risk(
        route_id=route,
        stop_id=stop,
        trip_id=trip,
        departure_time_str=departure,
        query_dt=query_dt,
        historical_reliability=hist,
    )


class TestComputeLiveRisk:

    def test_no_rt_state_gives_neutral_risk(self):
        """With no GTFS-RT data the score is simply 1 - historical_reliability."""
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(hist=0.8)

        assert result["risk_score"] == pytest.approx(0.2, abs=1e-9)
        assert result["risk_label"] == "Low"
        assert result["modifiers"] == []
        assert result["is_cancelled"] is False

    def test_cancelled_trip_returns_max_risk(self):
        cancelled = TripUpdateState(trip_id="T1", route_id="R1", is_cancelled=True)
        with patch(f"{_LIVE}.trip_updates", {"T1": cancelled}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(trip="T1")

        assert result["risk_score"] == 1.0
        assert result["risk_label"] == "High"
        assert result["is_cancelled"] is True

    def test_late_evening_bumps_risk(self):
        """Departure after 22:00 should add LATE_EVENING_RISK_BUMP."""
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(
                departure="22:30:00",
                query_dt=datetime(2026, 2, 9, 22, 0),
                hist=0.8,
            )

        expected = pytest.approx(0.2 + LATE_EVENING_RISK_BUMP, abs=1e-9)
        assert result["risk_score"] == expected
        assert any("22:00" in m or "late" in m.lower() for m in result["modifiers"])

    def test_weekend_bumps_risk(self):
        """Weekend query should add WEEKEND_RISK_BUMP."""
        saturday = datetime(2026, 2, 7, 14, 0)
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(query_dt=saturday, hist=0.8)

        expected = pytest.approx(0.2 + WEEKEND_RISK_BUMP, abs=1e-9)
        assert result["risk_score"] == expected
        assert any("weekend" in m.lower() for m in result["modifiers"])

    def test_active_alert_bumps_risk(self):
        """A service alert on the route should add ALERT_RISK_BUMP."""
        alert = ServiceAlertState(
            alert_id="A1",
            header="Delay on route R1",
            description="Operational issues",
            affected_route_ids=["R1"],
        )
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", [alert]), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(hist=0.8)

        expected = pytest.approx(0.2 + ALERT_RISK_BUMP, abs=1e-9)
        assert result["risk_score"] == expected
        assert any("alert" in m.lower() for m in result["modifiers"])

    def test_same_route_cancellation_bumps_risk(self):
        """Earlier cancellation on the same route should add CANCELLATION_RISK_BUMP."""
        other_cancelled = TripUpdateState(trip_id="T99", route_id="R1", is_cancelled=True)
        with patch(f"{_LIVE}.trip_updates", {"T99": other_cancelled}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(route="R1", trip="T1", hist=0.8)  # T1 != T99

        expected = pytest.approx(0.2 + CANCELLATION_RISK_BUMP, abs=1e-9)
        assert result["risk_score"] == expected
        assert any("cancellation" in m.lower() for m in result["modifiers"])

    def test_missing_vehicle_position_bumps_risk(self):
        """No vehicle position within 15 min of departure adds MISSING_VEHICLE_RISK_BUMP."""
        # Departure at 13:10, query at 13:00 (10 min before — within window)
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(
                departure="13:10:00",
                query_dt=datetime(2026, 2, 9, 13, 0),
                hist=0.8,
                trip="T1",
            )

        expected = pytest.approx(0.2 + MISSING_VEHICLE_RISK_BUMP, abs=1e-9)
        assert result["risk_score"] == expected

    def test_vehicle_present_no_bump(self):
        """Vehicle position present — no missing vehicle bump."""
        vp = {"T1": {"lat": 43.6, "lon": -79.4, "timestamp": 1234}}
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", vp):
            result = _compute(
                departure="13:10:00",
                query_dt=datetime(2026, 2, 9, 13, 0),
                hist=0.8,
                trip="T1",
            )

        assert result["risk_score"] == pytest.approx(0.2, abs=1e-9)

    def test_risk_capped_at_1(self):
        """Multiple modifiers cannot push the score above 1.0."""
        saturday = datetime(2026, 2, 7, 22, 30)
        alert = ServiceAlertState(
            alert_id="A1", header="Major disruption", description="",
            affected_route_ids=["R1"],
        )
        other_cancelled = TripUpdateState(trip_id="T99", route_id="R1", is_cancelled=True)
        with patch(f"{_LIVE}.trip_updates", {"T99": other_cancelled}), \
             patch(f"{_LIVE}.service_alerts", [alert, alert]), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="22:30:00",
                query_dt=saturday,
                historical_reliability=0.0,  # worst possible prior
            )

        assert result["risk_score"] <= 1.0

    def test_risk_label_thresholds(self):
        """Verify Low < 0.33, Medium < 0.66, High ≥ 0.66."""
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            low = _compute(hist=0.8)
            mid = _compute(hist=0.45)
            high = _compute(hist=0.1)

        assert low["risk_label"] == "Low"
        assert mid["risk_label"] == "Medium"
        assert high["risk_label"] == "High"
