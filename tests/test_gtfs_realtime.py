"""
Unit tests for ingestion.gtfs_realtime.observe_departures().

Each test patches the module-level state dicts and _recorded_today/_recorded_date
directly so no real HTTP calls or scheduler are needed.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, Route, Stop, StopTime, Trip
import ingestion.gtfs_realtime as rt_mod
from ingestion.gtfs_realtime import TripUpdateState, observe_departures


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def obs_db():
    """Minimal SQLite DB seeded with one route, two trips, and stop times."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    session.add(Stop(stop_id="S1", stop_name="Stop 1", stop_lat=43.0, stop_lon=-79.0))
    session.add(Stop(stop_id="S2", stop_name="Stop 2", stop_lat=43.1, stop_lon=-79.1))
    session.add(Route(route_id="R1", route_short_name="1", route_long_name="Test", route_type=3))

    # Trip that ran yesterday at 08:00 and 08:30 (service_id = yesterday's date)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    session.add(Trip(trip_id="T_past", route_id="R1", service_id=yesterday, trip_headsign="GL", direction_id=0))
    session.add(StopTime(trip_id="T_past", stop_id="S1", stop_sequence=1, departure_time="08:00:00", arrival_time="08:00:00"))
    session.add(StopTime(trip_id="T_past", stop_id="S2", stop_sequence=2, departure_time="08:30:00", arrival_time="08:30:00"))

    # Trip scheduled far in the future (tomorrow at 23:00)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d")
    session.add(Trip(trip_id="T_future", route_id="R1", service_id=tomorrow, trip_headsign="GL", direction_id=0))
    session.add(StopTime(trip_id="T_future", stop_id="S1", stop_sequence=1, departure_time="23:00:00", arrival_time="23:00:00"))
    session.add(StopTime(trip_id="T_future", stop_id="S2", stop_sequence=2, departure_time="23:30:00", arrival_time="23:30:00"))

    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def reset_rt_state():
    """Reset module-level RT state before each test."""
    rt_mod.trip_updates.clear()
    rt_mod._recorded_today = set()
    rt_mod._recorded_date = ""
    yield
    rt_mod.trip_updates.clear()
    rt_mod._recorded_today = set()
    rt_mod._recorded_date = ""


# ---------------------------------------------------------------------------
# observe_departures tests
# ---------------------------------------------------------------------------

class TestObserveDepartures:
    def test_cancelled_trip_records_all_stops(self, obs_db):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        rt_mod.trip_updates["T_past"] = TripUpdateState(
            trip_id="T_past", route_id="R1", is_cancelled=True
        )
        with patch("ingestion.gtfs_realtime.record_observed_departure") as mock_record:
            count = observe_departures(obs_db)

        assert count == 2  # S1 and S2 both recorded
        assert mock_record.call_count == 2
        calls = {call.kwargs["stop_id"] for call in mock_record.call_args_list}
        assert calls == {"S1", "S2"}
        assert all(call.kwargs["was_cancelled"] is True for call in mock_record.call_args_list)

    def test_cancelled_trip_added_to_recorded_today(self, obs_db):
        rt_mod.trip_updates["T_past"] = TripUpdateState(
            trip_id="T_past", route_id="R1", is_cancelled=True
        )
        with patch("ingestion.gtfs_realtime.record_observed_departure"):
            observe_departures(obs_db)
        assert "T_past" in rt_mod._recorded_today

    def test_already_recorded_trip_is_skipped(self, obs_db):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        rt_mod._recorded_today = {"T_past"}
        rt_mod._recorded_date = today
        rt_mod.trip_updates["T_past"] = TripUpdateState(
            trip_id="T_past", route_id="R1", is_cancelled=True
        )
        with patch("ingestion.gtfs_realtime.record_observed_departure") as mock_record:
            count = observe_departures(obs_db)

        assert count == 0
        mock_record.assert_not_called()

    def test_trip_not_in_static_schedule_is_skipped(self, obs_db):
        rt_mod.trip_updates["T_unknown"] = TripUpdateState(
            trip_id="T_unknown", route_id="R1", is_cancelled=True
        )
        with patch("ingestion.gtfs_realtime.record_observed_departure") as mock_record:
            count = observe_departures(obs_db)

        assert count == 0
        mock_record.assert_not_called()

    def test_delayed_trip_only_records_past_stops(self, obs_db):
        # T_past has S1 at 08:00 (past) and S2 at 08:30 (past).
        # Override only S1 with delay data — S2 has no RT override and should be skipped.
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        rt_mod.trip_updates["T_past"] = TripUpdateState(
            trip_id="T_past",
            route_id="R1",
            is_cancelled=False,
            stop_time_overrides={"S1": 120},  # S1 delayed 2 minutes; S2 has no override
        )
        with patch("ingestion.gtfs_realtime.record_observed_departure") as mock_record:
            count = observe_departures(obs_db)

        assert count == 1
        assert mock_record.call_count == 1
        assert mock_record.call_args.kwargs["stop_id"] == "S1"
        assert mock_record.call_args.kwargs["delay_seconds"] == 120
        assert mock_record.call_args.kwargs["was_cancelled"] is False

    def test_future_stop_with_override_not_recorded(self, obs_db):
        # T_future departs tomorrow at 23:00 — scheduled_at > now, so should not record
        rt_mod.trip_updates["T_future"] = TripUpdateState(
            trip_id="T_future",
            route_id="R1",
            is_cancelled=False,
            stop_time_overrides={"S1": 60, "S2": 60},
        )
        with patch("ingestion.gtfs_realtime.record_observed_departure") as mock_record:
            count = observe_departures(obs_db)

        assert count == 0
        mock_record.assert_not_called()

    def test_date_rollover_clears_recorded_set(self, obs_db):
        # Simulate yesterday's recorded set — should be cleared when date changes
        rt_mod._recorded_today = {"T_past"}
        rt_mod._recorded_date = "19990101"  # clearly a past date

        rt_mod.trip_updates["T_past"] = TripUpdateState(
            trip_id="T_past", route_id="R1", is_cancelled=True
        )
        with patch("ingestion.gtfs_realtime.record_observed_departure"):
            count = observe_departures(obs_db)

        # _recorded_today was wiped on date change, so T_past was processed
        assert count == 2
