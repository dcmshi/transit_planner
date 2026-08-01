"""
Unit tests for the reliability modules.

reliability.historical — pure functions only (classify_time_bucket).
reliability.live       — compute_live_risk, which reads module-level
                         GTFS-RT state; patched via unittest.mock.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config import AGENCY_TZ
from db.models import Base, ReliabilityRecord
from ingestion.gtfs_realtime import ServiceAlertState, TripUpdateState
from reliability.historical import (
    NEUTRAL_PRIOR,
    ReliabilitySnapshot,
    classify_time_bucket,
    get_historical_reliability,
    get_historical_reliability_batch,
    record_observed_departure,
)
from reliability.live import (
    ALERT_RISK_BUMP,
    CANCELLATION_RISK_BUMP,
    LATE_EVENING_RISK_BUMP,
    MAX_ALERT_RISK_BUMP,
    MISSING_VEHICLE_RISK_BUMP,
    RISK_LABEL_HIGH_AT,
    RISK_LABEL_MEDIUM_AT,
    WEEKEND_RISK_BUMP,
    compute_live_risk,
    risk_label,
)

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

    def test_alert_outside_its_active_period_is_ignored(self):
        """An alert covering only today must not inflate a trip weeks out.
        Every other live signal is gated on the service day; the alert bump
        was not, and active_period was never parsed at all."""
        travel = datetime(2026, 2, 9, 14, 0, tzinfo=AGENCY_TZ)
        alert = ServiceAlertState(
            alert_id="A1",
            header="Closure on route R1",
            description="",
            affected_route_ids=["R1"],
            active_periods=[(
                travel - timedelta(days=21),
                travel - timedelta(days=20),
            )],
        )
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", [alert]), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(hist=0.8)

        assert result["risk_score"] == pytest.approx(0.2, abs=1e-9)
        assert result["modifiers"] == []

    def test_alert_inside_its_active_period_still_bumps(self):
        travel = datetime(2026, 2, 9, 14, 0, tzinfo=AGENCY_TZ)
        alert = ServiceAlertState(
            alert_id="A1",
            header="Closure on route R1",
            description="",
            affected_route_ids=["R1"],
            active_periods=[(travel - timedelta(hours=2), travel + timedelta(hours=2))],
        )
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", [alert]), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(hist=0.8)

        assert result["risk_score"] == pytest.approx(0.2 + ALERT_RISK_BUMP, abs=1e-9)

    def test_alert_with_no_active_period_is_always_active(self):
        """GTFS-RT: an absent active_period means the alert always applies."""
        alert = ServiceAlertState(
            alert_id="A1", header="Ongoing", description="",
            affected_route_ids=["R1"], active_periods=[],
        )
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", [alert]), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(hist=0.8)

        assert result["risk_score"] == pytest.approx(0.2 + ALERT_RISK_BUMP, abs=1e-9)

    def test_open_ended_active_period_bounds_are_honoured(self):
        travel = datetime(2026, 2, 9, 14, 0, tzinfo=AGENCY_TZ)
        started = ServiceAlertState(
            alert_id="A1", header="Started, no end", description="",
            affected_route_ids=["R1"],
            active_periods=[(travel - timedelta(days=1), None)],
        )
        not_yet = ServiceAlertState(
            alert_id="A2", header="Future, no end", description="",
            affected_route_ids=["R1"],
            active_periods=[(travel + timedelta(days=1), None)],
        )
        for alert, expected in ((started, ALERT_RISK_BUMP), (not_yet, 0.0)):
            with patch(f"{_LIVE}.trip_updates", {}), \
                 patch(f"{_LIVE}.service_alerts", [alert]), \
                 patch(f"{_LIVE}.vehicle_positions", {}):
                result = _compute(hist=0.8)
            assert result["risk_score"] == pytest.approx(0.2 + expected, abs=1e-9)

    def test_alert_bump_is_capped(self):
        """Ten alerts on one stop must not saturate the score on that signal
        alone — the bump was previously unbounded."""
        alerts = [
            ServiceAlertState(alert_id=f"A{i}", header=f"Alert {i}", description="",
                              affected_route_ids=["R1"])
            for i in range(10)
        ]
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", alerts), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(hist=0.8)

        assert result["risk_score"] == pytest.approx(0.2 + MAX_ALERT_RISK_BUMP, abs=1e-9)
        assert len(result["modifiers"]) == 10  # still all reported to the rider

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

    def test_cross_midnight_departure_uses_gtfs_convention(self):
        """A post-midnight departure ("24:05:00") queried at 23:55 is 10 min
        away — the missing-vehicle window must work across midnight via the
        GTFS >24:00:00 convention."""
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(
                departure="24:05:00",
                query_dt=datetime(2026, 2, 9, 23, 55),  # Monday
                hist=0.8,
                trip="T1",
            )

        # 10 min to departure, no vehicle position → bump; also late evening.
        expected = pytest.approx(
            0.2 + MISSING_VEHICLE_RISK_BUMP + LATE_EVENING_RISK_BUMP, abs=1e-9
        )
        assert result["risk_score"] == expected

    def test_cross_midnight_departure_outside_vehicle_window(self):
        """"25:30:00" (1:30 AM next day) queried at 23:50 is 100 min away —
        no missing-vehicle bump, late-evening bump only."""
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = _compute(
                departure="25:30:00",
                query_dt=datetime(2026, 2, 9, 23, 50),  # Monday
                hist=0.8,
                trip="T1",
            )

        expected = pytest.approx(0.2 + LATE_EVENING_RISK_BUMP, abs=1e-9)
        assert result["risk_score"] == expected

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

    def test_weekend_bump_uses_travel_date_not_query_date(self):
        """Regression: a Friday query for Saturday travel gets the weekend
        bump (old code keyed the bump to the query's weekday)."""
        friday_query = datetime(2026, 2, 6, 14, 0)     # Friday
        saturday_dep = datetime(2026, 2, 7, 14, 0)     # Saturday travel
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="14:00:00",
                query_dt=friday_query,
                historical_reliability=0.8,
                scheduled_dt=saturday_dep,
            )

        expected = pytest.approx(0.2 + WEEKEND_RISK_BUMP, abs=1e-9)
        assert result["risk_score"] == expected
        assert any("weekend" in m.lower() for m in result["modifiers"])

    def test_no_weekend_bump_for_weekday_travel_queried_on_weekend(self):
        saturday_query = datetime(2026, 2, 7, 14, 0)   # Saturday
        monday_dep = datetime(2026, 2, 9, 14, 0)       # Monday travel
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="14:00:00",
                query_dt=saturday_query,
                historical_reliability=0.8,
                scheduled_dt=monday_dep,
            )

        assert result["risk_score"] == pytest.approx(0.2, abs=1e-9)
        assert not any("weekend" in m.lower() for m in result["modifiers"])

    def test_missing_vehicle_window_not_applied_to_future_dates(self):
        """Regression: a trip departing 10 minutes past the query's wall
        clock — but tomorrow — must not get the missing-vehicle bump (old
        code compared seconds-past-midnight only)."""
        query = datetime(2026, 2, 9, 13, 0)            # Monday 13:00
        tomorrow_dep = datetime(2026, 2, 10, 13, 10)   # Tuesday 13:10
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="13:10:00",
                query_dt=query,
                historical_reliability=0.8,
                scheduled_dt=tomorrow_dep,
            )

        assert result["risk_score"] == pytest.approx(0.2, abs=1e-9)
        assert result["modifiers"] == []

    def test_running_late_bumps_risk_tiered(self):
        from reliability.live import DELAY_RISK_BUMP_MAJOR, DELAY_RISK_BUMP_MINOR

        minor = TripUpdateState(trip_id="T1", route_id="R1", delay_seconds=6 * 60)
        major = TripUpdateState(trip_id="T1", route_id="R1", delay_seconds=20 * 60)

        with patch(f"{_LIVE}.trip_updates", {"T1": minor}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {"T1": {}}):
            minor_result = _compute(hist=0.8)
        with patch(f"{_LIVE}.trip_updates", {"T1": major}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {"T1": {}}):
            major_result = _compute(hist=0.8)

        assert minor_result["risk_score"] == pytest.approx(0.2 + DELAY_RISK_BUMP_MINOR, abs=1e-9)
        assert major_result["risk_score"] == pytest.approx(0.2 + DELAY_RISK_BUMP_MAJOR, abs=1e-9)
        assert any("late" in m.lower() for m in major_result["modifiers"])

    def test_stop_override_takes_precedence_for_delay(self):
        from reliability.live import DELAY_RISK_BUMP_MAJOR

        # Overall delay small, but this stop's override is 20 min late.
        tu = TripUpdateState(trip_id="T1", route_id="R1", delay_seconds=60,
                             stop_time_overrides={"S1": 20 * 60})
        with patch(f"{_LIVE}.trip_updates", {"T1": tu}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {"T1": {}}):
            result = _compute(stop="S1", hist=0.8)

        assert result["risk_score"] == pytest.approx(0.2 + DELAY_RISK_BUMP_MAJOR, abs=1e-9)

    def test_live_signals_do_not_leak_onto_future_dates(self):
        """Regression: trip_ids repeat across service days — today's
        cancellation/delay must not mark tomorrow's run of the same
        trip_id."""
        cancelled = TripUpdateState(trip_id="T1", route_id="R1", is_cancelled=True)
        query = datetime(2026, 2, 9, 13, 0)
        tomorrow_dep = datetime(2026, 2, 10, 14, 0)
        with patch(f"{_LIVE}.trip_updates", {"T1": cancelled}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="14:00:00",
                query_dt=query,
                historical_reliability=0.8,
                scheduled_dt=tomorrow_dep,
            )

        # Neither cancelled nor bumped by today's same-route cancellation.
        assert result["is_cancelled"] is False
        assert result["risk_score"] == pytest.approx(0.2, abs=1e-9)

    def test_post_midnight_leg_still_gets_same_day_live_signals(self):
        """Regression: a 25:30 leg rolls scheduled_dt onto tomorrow's date,
        but the bus belongs to today's service and is live in today's RT
        snapshot — the service_date gate must keep its cancellation
        visible."""
        from datetime import date

        cancelled = TripUpdateState(trip_id="T1", route_id="R1", is_cancelled=True)
        query = datetime(2026, 2, 9, 23, 50)               # Monday night
        rolled_dep = datetime(2026, 2, 10, 1, 30)          # 25:30 → Tue 01:30
        with patch(f"{_LIVE}.trip_updates", {"T1": cancelled}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            result = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="25:30:00",
                query_dt=query,
                historical_reliability=0.8,
                scheduled_dt=rolled_dep,
                service_date=date(2026, 2, 9),  # Monday's service
            )

        assert result["is_cancelled"] is True
        assert result["risk_score"] == 1.0

    def test_get_live_delay_lookup(self):
        from reliability.live import get_live_delay

        tu = TripUpdateState(trip_id="T1", route_id="R1", delay_seconds=120,
                             stop_time_overrides={"S1": 300})
        cancelled = TripUpdateState(trip_id="T2", route_id="R1", is_cancelled=True)
        with patch(f"{_LIVE}.trip_updates", {"T1": tu, "T2": cancelled}):
            assert get_live_delay("T1", "S1") == 300   # stop override wins
            assert get_live_delay("T1", "S9") == 120   # falls back to trip delay
            assert get_live_delay("T2", "S1") is None  # cancelled
            assert get_live_delay("T9", "S1") is None  # unknown trip

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

    def test_risk_label_boundaries_are_exact(self):
        assert risk_label(RISK_LABEL_MEDIUM_AT - 1e-9) == "Low"
        assert risk_label(RISK_LABEL_MEDIUM_AT) == "Medium"
        assert risk_label(RISK_LABEL_HIGH_AT - 1e-9) == "Medium"
        assert risk_label(RISK_LABEL_HIGH_AT) == "High"
        assert risk_label(0.0) == "Low"
        assert risk_label(1.0) == "High"

    def test_api_route_label_uses_the_same_helper(self):
        """api/routes.py re-implemented these thresholds inline, so the
        route-level label could drift from the leg-level one.  Both must now
        agree at every boundary."""
        import api.routes as routes_mod

        assert routes_mod.risk_label is risk_label
        for score in (0.0, 0.32, RISK_LABEL_MEDIUM_AT, 0.5,
                      RISK_LABEL_HIGH_AT, 0.9, 1.0):
            leg = _compute(hist=1.0 - score)
            assert leg["risk_label"] == risk_label(score)


# ---------------------------------------------------------------------------
# Shared DB fixture for historical functions
# ---------------------------------------------------------------------------

@pytest.fixture
def hist_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# get_historical_reliability_batch
# ---------------------------------------------------------------------------

class TestGetHistoricalReliabilityBatch:
    def test_matches_single_lookups(self, hist_db):
        from reliability.historical import get_historical_reliability_batch

        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            scheduled_departures=100, observed_departures=80,
            cancellation_count=4, total_delay_seconds=6000,
            updated_at="2026-07-01T00:00:00+00:00",
        ))
        hist_db.add(ReliabilityRecord(
            route_id="R2", stop_id="S2", time_bucket="weekend",
            scheduled_departures=10, observed_departures=10,
            cancellation_count=0, total_delay_seconds=0,
            updated_at="2026-07-01T00:00:00+00:00",
        ))
        hist_db.commit()

        keys = [
            ("R1", "S1", "weekday_am_peak"),
            ("R2", "S2", "weekend"),
            ("R9", "S9", "weekday_offpeak"),  # no record
        ]
        batch = get_historical_reliability_batch(keys, hist_db)

        for key in keys[:2]:
            assert batch[key] == pytest.approx(
                get_historical_reliability(*key, hist_db)
            )
        assert ("R9", "S9", "weekday_offpeak") not in batch  # caller uses prior

    def test_newest_record_wins_duplicates(self, hist_db):
        from reliability.historical import get_historical_reliability_batch

        for updated, observed in [("2026-07-01T00:00:00", 0), ("2026-07-09T00:00:00", 10)]:
            hist_db.add(ReliabilityRecord(
                route_id="R1", stop_id="S1", time_bucket="weekend",
                scheduled_departures=10, observed_departures=observed,
                cancellation_count=0, total_delay_seconds=0,
                updated_at=updated,
            ))
        hist_db.commit()

        batch = get_historical_reliability_batch([("R1", "S1", "weekend")], hist_db)
        # Newest record (perfect 10/10) wins, matching the single lookup.
        assert batch[("R1", "S1", "weekend")] == pytest.approx(1.0)

    def test_empty_keys(self, hist_db):
        from reliability.historical import get_historical_reliability_batch
        assert get_historical_reliability_batch([], hist_db) == {}


# ---------------------------------------------------------------------------
# decay_reliability_records — rolling-window enforcement
# ---------------------------------------------------------------------------

class TestDecayReliabilityRecords:
    @pytest.fixture(autouse=True)
    def _reset_decay_guard(self):
        import reliability.historical as hist_mod
        hist_mod._last_decay_date = ""
        yield
        hist_mod._last_decay_date = ""

    def _seed(self, db, scheduled=100, observed=80, delay=6000, cancels=4):
        db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            scheduled_departures=scheduled, observed_departures=observed,
            cancellation_count=cancels, total_delay_seconds=delay,
        ))
        db.commit()

    def test_counters_halve_after_one_half_life(self, hist_db):
        from reliability.historical import WINDOW_DAYS, decay_reliability_records

        self._seed(hist_db)
        updated = decay_reliability_records(hist_db, days_elapsed=WINDOW_DAYS)

        assert updated == 1
        rec = hist_db.query(ReliabilityRecord).one()
        assert rec.scheduled_departures == pytest.approx(50)
        assert rec.observed_departures == pytest.approx(40)
        assert rec.total_delay_seconds == pytest.approx(3000)
        assert rec.cancellation_count == pytest.approx(2)

    def test_decay_preserves_score(self, hist_db):
        """Uniform scaling keeps observed_rate/cancel_rate/avg-delay — the
        score must not change just because time passed."""
        from reliability.historical import WINDOW_DAYS, decay_reliability_records

        self._seed(hist_db)
        before = get_historical_reliability("R1", "S1", "weekday_am_peak", hist_db)
        decay_reliability_records(hist_db, days_elapsed=WINDOW_DAYS)
        after = get_historical_reliability("R1", "S1", "weekday_am_peak", hist_db)

        assert after == pytest.approx(before, abs=0.02)  # integer rounding only

    def test_decay_runs_at_most_once_per_day(self, hist_db):
        from reliability.historical import decay_reliability_records

        self._seed(hist_db)
        assert decay_reliability_records(hist_db) == 1
        assert decay_reliability_records(hist_db) == 0  # same-day guard

        rec = hist_db.query(ReliabilityRecord).one()
        # One day of decay only (factor 0.5**(1/14) ≈ 0.9517)
        assert rec.scheduled_departures == pytest.approx(100 * 0.5 ** (1 / 14), rel=1e-6)

    def test_small_counters_actually_decay(self, hist_db):
        """Regression: with integer ROUND, every counter <= 10 was a fixed
        point — a single recorded no-show (scheduled=1, observed=0) scored
        risk 1.0 forever.  Float counters must keep shrinking."""
        import reliability.historical as hist_mod
        from reliability.historical import WINDOW_DAYS, decay_reliability_records

        self._seed(hist_db, scheduled=1, observed=0, delay=0, cancels=0)
        decay_reliability_records(hist_db, days_elapsed=WINDOW_DAYS)

        rec = hist_db.query(ReliabilityRecord).one()
        assert rec.scheduled_departures == pytest.approx(0.5)

        # After another half-life it fades below the minimum sample and is
        # purged — the score falls back to the neutral prior.
        hist_mod._last_decay_date = ""  # bypass the once-per-day guard
        decay_reliability_records(hist_db, days_elapsed=WINDOW_DAYS)
        assert hist_db.query(ReliabilityRecord).count() == 0
        score = get_historical_reliability("R1", "S1", "weekday_am_peak", hist_db)
        assert score == pytest.approx(0.8)

    def test_faded_record_scores_neutral_prior_before_purge(self, hist_db):
        """A record already below the minimum sample must not be scored."""
        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            scheduled_departures=0.3, observed_departures=0.0,
            cancellation_count=0.0, total_delay_seconds=0.0,
        ))
        hist_db.commit()
        score = get_historical_reliability("R1", "S1", "weekday_am_peak", hist_db)
        assert score == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# get_historical_reliability
# ---------------------------------------------------------------------------

class TestGetHistoricalReliability:

    def test_returns_neutral_prior_when_no_data(self, hist_db):
        score = get_historical_reliability("R1", "S1", "weekday_am_peak", hist_db)
        assert score == pytest.approx(0.8)

    def test_returns_neutral_prior_when_zero_scheduled(self, hist_db):
        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            scheduled_departures=0, observed_departures=0,
            cancellation_count=0, total_delay_seconds=0,
        ))
        hist_db.commit()
        score = get_historical_reliability("R1", "S1", "weekday_am_peak", hist_db)
        assert score == pytest.approx(0.8)

    def test_perfect_record_returns_high_score(self, hist_db):
        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            scheduled_departures=100, observed_departures=100,
            cancellation_count=0, total_delay_seconds=0,
        ))
        hist_db.commit()
        score = get_historical_reliability("R1", "S1", "weekday_am_peak", hist_db)
        assert score == pytest.approx(1.0)

    def test_all_cancelled_returns_low_score(self, hist_db):
        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_offpeak",
            scheduled_departures=10, observed_departures=0,
            cancellation_count=10, total_delay_seconds=0,
        ))
        hist_db.commit()
        score = get_historical_reliability("R1", "S1", "weekday_offpeak", hist_db)
        assert score == pytest.approx(0.0)

    def test_bucket_mismatch_returns_neutral_prior(self, hist_db):
        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            scheduled_departures=50, observed_departures=50,
            cancellation_count=0, total_delay_seconds=0,
        ))
        hist_db.commit()
        # Different bucket — no data → neutral prior
        score = get_historical_reliability("R1", "S1", "weekend", hist_db)
        assert score == pytest.approx(0.8)

    def test_delay_reduces_score(self, hist_db):
        # 30-min average delay applies maximum delay penalty (0.2)
        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_pm_peak",
            scheduled_departures=10, observed_departures=10,
            cancellation_count=0, total_delay_seconds=10 * 30 * 60,  # 30-min avg
        ))
        hist_db.commit()
        score = get_historical_reliability("R1", "S1", "weekday_pm_peak", hist_db)
        # observed_rate=1.0, cancel_rate=0.0, delay_penalty=0.2 → 0.8
        assert score == pytest.approx(0.8, abs=1e-6)

    def test_null_counters_return_neutral_prior(self, hist_db):
        """The four counter columns are nullable, so a row written by raw SQL
        (or an ALTER that back-filled nothing) can hold NULL.  That used to
        raise TypeError comparing None < _MIN_SCHEDULED."""
        hist_db.execute(text(
            "INSERT INTO reliability_records "
            "(route_id, stop_id, time_bucket, source) VALUES "
            "('R1', 'S1', 'weekday_am_peak', 'observed')"
        ))
        hist_db.commit()
        score = get_historical_reliability("R1", "S1", "weekday_am_peak", hist_db)
        assert score == pytest.approx(0.8)

    def test_null_counters_skipped_by_batch_lookup(self, hist_db):
        from reliability.historical import get_historical_reliability_batch

        hist_db.execute(text(
            "INSERT INTO reliability_records "
            "(route_id, stop_id, time_bucket, source) VALUES "
            "('R1', 'S1', 'weekday_am_peak', 'observed')"
        ))
        hist_db.commit()
        scores = get_historical_reliability_batch(
            [("R1", "S1", "weekday_am_peak")], hist_db
        )
        assert scores == {}


# ---------------------------------------------------------------------------
# record_observed_departure
# ---------------------------------------------------------------------------

class TestRecordObservedDeparture:

    _AM_DT = datetime(2026, 2, 9, 8, 0, tzinfo=timezone.utc)   # weekday AM peak
    _PM_DT = datetime(2026, 2, 9, 16, 0, tzinfo=timezone.utc)  # weekday PM peak

    def test_creates_new_record_when_none_exists(self, hist_db):
        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=0,
            was_cancelled=False, session=hist_db,
        )
        rec = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak"
        ).first()
        assert rec is not None
        assert rec.scheduled_departures == 1
        assert rec.observed_departures == 1

    def test_updates_existing_record(self, hist_db):
        # Seed one record
        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=0,
            was_cancelled=False, session=hist_db,
        )
        # Second observation
        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=120,
            was_cancelled=False, session=hist_db,
        )
        rec = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).first()
        assert rec.scheduled_departures == 2
        assert rec.observed_departures == 2
        assert rec.total_delay_seconds == 120

    def test_cancellation_increments_cancellation_count(self, hist_db):
        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=0,
            was_cancelled=True, session=hist_db,
        )
        rec = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).first()
        assert rec.cancellation_count == 1
        assert rec.observed_departures == 0  # not a successful departure

    def test_normal_departure_does_not_increment_cancellation(self, hist_db):
        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=60,
            was_cancelled=False, session=hist_db,
        )
        rec = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).first()
        assert rec.cancellation_count == 0
        assert rec.total_delay_seconds == 60

    def test_assigns_correct_time_bucket_from_scheduled_at(self, hist_db):
        record_observed_departure(
            "R1", "S1", self._PM_DT, delay_seconds=0,
            was_cancelled=False, session=hist_db,
        )
        rec = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).first()
        assert rec.time_bucket == "weekday_pm_peak"

    def test_separate_buckets_kept_separate(self, hist_db):
        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=0,
            was_cancelled=False, session=hist_db,
        )
        record_observed_departure(
            "R1", "S1", self._PM_DT, delay_seconds=0,
            was_cancelled=False, session=hist_db,
        )
        records = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).all()
        assert len(records) == 2
        buckets = {r.time_bucket for r in records}
        assert buckets == {"weekday_am_peak", "weekday_pm_peak"}

    def test_new_record_tagged_observed(self, hist_db):
        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=0,
            was_cancelled=False, session=hist_db,
        )
        rec = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).first()
        assert rec.source == "observed"

    def test_seeded_record_flips_to_mixed_on_real_observation(self, hist_db):
        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            scheduled_departures=100, observed_departures=85,
            total_delay_seconds=0, cancellation_count=3,
            window_start_date="20260201", source="seed",
        ))
        hist_db.commit()

        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=0,
            was_cancelled=False, session=hist_db,
        )
        rec = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).first()
        assert rec.source == "mixed"
        assert rec.scheduled_departures == 101  # blended, not reset

    def test_window_end_date_updated(self, hist_db):
        record_observed_departure(
            "R1", "S1", self._AM_DT, delay_seconds=0,
            was_cancelled=False, session=hist_db,
        )
        rec = hist_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).first()
        assert rec.window_end_date == "20260209"


class TestRiskTimeBucket:
    """LiveRisk reports which bucket produced its score, so a client can pull
    the matching /reliability row instead of re-deriving the bucketing rules."""

    def _bucket(self, **kw):
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            return _compute(**kw)["time_bucket"]

    @pytest.mark.parametrize("departure,query,expected", [
        ("08:00:00", datetime(2026, 2, 9, 7, 0), "weekday_am_peak"),
        ("12:00:00", datetime(2026, 2, 9, 7, 0), "weekday_offpeak"),
        ("17:00:00", datetime(2026, 2, 9, 7, 0), "weekday_pm_peak"),
        ("12:00:00", datetime(2026, 2, 7, 7, 0), "weekend"),
    ])
    def test_matches_classify_time_bucket(self, departure, query, expected):
        assert self._bucket(departure=departure, query_dt=query) == expected

    def test_agrees_with_the_lookup_key_the_api_builds(self):
        """The API keys its historical lookup on classify_time_bucket of the
        leg's scheduled datetime; the reported bucket has to be that same one
        or the client fetches the wrong /reliability row."""
        leg_dt = datetime(2026, 2, 9, 17, 30)
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            risk = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="17:30:00",
                query_dt=datetime(2026, 2, 9, 8, 0),
                historical_reliability=0.8,
                scheduled_dt=leg_dt,
            )
        assert risk["time_bucket"] == classify_time_bucket(leg_dt)

    def test_present_when_the_neutral_prior_was_used(self):
        """Acceptance: the UI must be able to say "no observations yet for
        this bucket" rather than showing nothing."""
        assert self._bucket(hist=NEUTRAL_PRIOR) == "weekday_offpeak"

    def test_present_on_a_cancelled_trip(self):
        """The cancellation short-circuit returns early — it must still say
        which bucket it looked at."""
        cancelled = TripUpdateState(trip_id="T1", route_id="R1", is_cancelled=True)
        with patch(f"{_LIVE}.trip_updates", {"T1": cancelled}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            risk = _compute(trip="T1")
        assert risk["is_cancelled"] is True
        assert risk["time_bucket"] == "weekday_offpeak"

    def test_uses_the_travel_day_not_the_query_day(self):
        """A Friday query for Saturday travel must report the weekend bucket,
        matching the row the weekend history came from."""
        saturday = datetime(2026, 2, 7, 14, 0)
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            risk = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="14:00:00",
                query_dt=datetime(2026, 2, 6, 9, 0),  # Friday
                historical_reliability=0.8,
                scheduled_dt=saturday,
            )
        assert risk["time_bucket"] == "weekend"


class TestRiskCounters:
    """LiveRisk carries the counters behind its score, so a client can explain
    a leg without a second request."""

    _SNAP = ReliabilitySnapshot(
        scheduled_departures=100.0, observed_departures=95.0,
        total_delay_seconds=1200.0, cancellation_count=2.0,
        source="mixed", score=0.93,
    )

    def _risk(self, snapshot):
        with patch(f"{_LIVE}.trip_updates", {}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            return compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="14:00:00",
                query_dt=datetime(2026, 2, 9, 13, 0),
                historical_reliability=0.93,
                reliability=snapshot,
            )

    def test_counters_come_from_the_snapshot(self):
        r = self._risk(self._SNAP)
        assert r["scheduled_departures"] == 100.0
        assert r["observed_departures"] == 95.0
        assert r["total_delay_seconds"] == 1200.0
        assert r["cancellation_count"] == 2.0
        assert r["source"] == "mixed"
        assert r["neutral_prior_used"] is False

    def test_absent_snapshot_reads_as_no_history(self):
        """No stored record at all — zeros and a neutral prior, not nulls the
        UI has to special-case."""
        r = self._risk(None)
        assert r["scheduled_departures"] == 0.0
        assert r["observed_departures"] == 0.0
        assert r["source"] is None
        assert r["neutral_prior_used"] is True

    def test_sparse_record_keeps_its_counters(self):
        """score=None means the neutral prior stood in, but the counters are
        still worth showing — that is how the UI says "only 0.3 departures
        recorded so far"."""
        sparse = self._SNAP._replace(scheduled_departures=0.3, score=None)
        r = self._risk(sparse)
        assert r["neutral_prior_used"] is True
        assert r["scheduled_departures"] == 0.3

    def test_counters_present_on_a_cancelled_trip(self):
        cancelled = TripUpdateState(trip_id="T1", route_id="R1", is_cancelled=True)
        with patch(f"{_LIVE}.trip_updates", {"T1": cancelled}), \
             patch(f"{_LIVE}.service_alerts", []), \
             patch(f"{_LIVE}.vehicle_positions", {}):
            r = compute_live_risk(
                route_id="R1", stop_id="S1", trip_id="T1",
                departure_time_str="14:00:00",
                query_dt=datetime(2026, 2, 9, 13, 0),
                historical_reliability=0.93,
                reliability=self._SNAP,
            )
        assert r["is_cancelled"] is True
        assert r["scheduled_departures"] == 100.0


class TestGetReliabilitySnapshots:
    def test_keeps_records_too_sparse_to_score(self, hist_db):
        """The score-only batch drops these; the snapshot keeps them so the
        API can show that there is no history yet."""
        from reliability.historical import get_reliability_snapshots

        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekend",
            scheduled_departures=0.2, observed_departures=0.1,
            cancellation_count=0, total_delay_seconds=0, source="seed",
        ))
        hist_db.commit()
        key = ("R1", "S1", "weekend")
        snaps = get_reliability_snapshots([key], hist_db)
        assert snaps[key].score is None
        assert snaps[key].scheduled_departures == 0.2
        assert get_historical_reliability_batch([key], hist_db) == {}

    def test_scores_agree_with_the_score_only_batch(self, hist_db):
        from reliability.historical import get_reliability_snapshots

        hist_db.add(ReliabilityRecord(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            scheduled_departures=100, observed_departures=95,
            cancellation_count=0, total_delay_seconds=0, source="observed",
        ))
        hist_db.commit()
        key = ("R1", "S1", "weekday_am_peak")
        assert (get_reliability_snapshots([key], hist_db)[key].score
                == get_historical_reliability_batch([key], hist_db)[key])

    def test_empty_keys(self, hist_db):
        from reliability.historical import get_reliability_snapshots
        assert get_reliability_snapshots([], hist_db) == {}
