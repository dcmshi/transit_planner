"""
Unit tests for ingestion.gtfs_realtime.observe_departures().

Each test patches the module-level state dicts and _recorded_today/_recorded_date
directly so no real HTTP calls or scheduler are needed.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import ingestion.gtfs_realtime as rt_mod
from config import AGENCY_TZ
from db.models import Base, Route, Stop, StopTime, Trip
from ingestion.gtfs_realtime import (
    TripUpdateState,
    observe_departures,
    poll_all,
    poll_service_alerts,
    poll_trip_updates,
    poll_vehicle_positions,
)

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

    # Trip that ran yesterday at 08:00 and 08:30 (service_id = yesterday's date,
    # agency-local — service days roll at agency midnight)
    yesterday = (datetime.now(AGENCY_TZ) - timedelta(days=1)).strftime("%Y%m%d")
    session.add(Trip(trip_id="T_past", route_id="R1", service_id=yesterday, trip_headsign="GL", direction_id=0))
    session.add(StopTime(trip_id="T_past", stop_id="S1", stop_sequence=1, departure_time="08:00:00", arrival_time="08:00:00"))
    session.add(StopTime(trip_id="T_past", stop_id="S2", stop_sequence=2, departure_time="08:30:00", arrival_time="08:30:00"))

    # Trip scheduled far in the future (tomorrow at 23:00)
    tomorrow = (datetime.now(AGENCY_TZ) + timedelta(days=1)).strftime("%Y%m%d")
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
    def _reset():
        rt_mod.trip_updates.clear()
        rt_mod.vehicle_positions.clear()
        rt_mod._recorded_today = set()
        rt_mod._recorded_date = ""
        rt_mod._seen_in_rt_today.clear()
        rt_mod._polling_since = None
        rt_mod._last_noshow_sweep = None

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# observe_departures tests
# ---------------------------------------------------------------------------

class TestObserveDepartures:
    def test_cancelled_trip_records_all_stops(self, obs_db):
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
        today = datetime.now(AGENCY_TZ).strftime("%Y%m%d")
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

    def test_delayed_trip_writes_db_state(self, obs_db):
        """End-to-end without mocks: a delayed trip produces a real
        ReliabilityRecord with the delay accumulated."""
        from db.models import ReliabilityRecord

        rt_mod.trip_updates["T_past"] = TripUpdateState(
            trip_id="T_past",
            route_id="R1",
            is_cancelled=False,
            stop_time_overrides={"S1": 120},
        )
        count = observe_departures(obs_db)

        assert count == 1
        rec = obs_db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1"
        ).first()
        assert rec is not None
        assert rec.scheduled_departures == 1
        assert rec.observed_departures == 1
        assert rec.total_delay_seconds == 120
        assert rec.cancellation_count == 0
        assert rec.source == "observed"

    def test_dedup_survives_restart(self, obs_db):
        """Persisted markers prevent double-counting after a process restart."""
        from db.models import ReliabilityRecord

        rt_mod.trip_updates["T_past"] = TripUpdateState(
            trip_id="T_past", route_id="R1", is_cancelled=True
        )
        count = observe_departures(obs_db)  # real record_observed_departure
        assert count == 2

        # Simulate a restart: all in-memory dedup state is lost.
        rt_mod._recorded_today = set()
        rt_mod._recorded_date = ""

        count_after_restart = observe_departures(obs_db)
        assert count_after_restart == 0  # markers reloaded from DB

        recs = obs_db.query(ReliabilityRecord).all()
        assert sum(r.cancellation_count for r in recs) == 2  # not doubled

    def test_stale_markers_purged_on_rollover(self, obs_db):
        """Markers from previous days are deleted during date rollover."""
        from db.models import ObservedTrip

        obs_db.add(ObservedTrip(trip_id="T_old", recorded_date="19990101"))
        obs_db.commit()

        observe_departures(obs_db)  # empty trip_updates; triggers rollover

        assert obs_db.query(ObservedTrip).filter_by(recorded_date="19990101").count() == 0


# ---------------------------------------------------------------------------
# record_no_shows — trips absent from every RT feed become misses
# ---------------------------------------------------------------------------

# Frozen instant: Wednesday 2026-07-08 15:00 agency-local (19:00 UTC, EDT).
_FROZEN_LOCAL = datetime(2026, 7, 8, 15, 0, tzinfo=AGENCY_TZ)
_FROZEN_UTC = _FROZEN_LOCAL.astimezone(timezone.utc)


class _FrozenDatetime(datetime):
    """datetime subclass whose now() is pinned to _FROZEN_UTC."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _FROZEN_UTC.replace(tzinfo=None)
        return _FROZEN_UTC.astimezone(tz)


@pytest.fixture
def frozen_now():
    with patch.object(rt_mod, "datetime", _FrozenDatetime):
        yield


def _add_todays_trip(session, trip_id, dep1="12:00:00", dep2="12:10:00"):
    """Seed a trip on the frozen day's service date with two stops."""
    today = _FROZEN_LOCAL.strftime("%Y%m%d")
    session.add(Trip(trip_id=trip_id, route_id="R1", service_id=today,
                     trip_headsign="GL", direction_id=0))
    session.add(StopTime(trip_id=trip_id, stop_id="S1", stop_sequence=1,
                         departure_time=dep1, arrival_time=dep1))
    session.add(StopTime(trip_id=trip_id, stop_id="S2", stop_sequence=2,
                         departure_time=dep2, arrival_time=dep2))
    session.commit()
    return today


class TestRecordNoShows:
    def _arm_polling(self, since_hours_before=6):
        """Simulate continuous RT coverage since N hours before frozen now."""
        rt_mod._polling_since = _FROZEN_UTC - timedelta(hours=since_hours_before)
        rt_mod._recorded_date = _FROZEN_LOCAL.strftime("%Y%m%d")

    def test_unseen_finished_trip_recorded_as_missed(self, obs_db, frozen_now):
        from db.models import ObservedTrip, ReliabilityRecord

        _add_todays_trip(obs_db, "T_ghost")  # ran 12:00–12:10, never seen
        self._arm_polling()

        count = rt_mod.record_no_shows(obs_db)

        assert count == 2  # both stops recorded as scheduled-but-missed
        recs = obs_db.query(ReliabilityRecord).all()
        assert sum(r.scheduled_departures for r in recs) == 2
        assert sum(r.observed_departures for r in recs) == 0
        assert sum(r.cancellation_count for r in recs) == 0
        marker = obs_db.query(ObservedTrip).filter_by(trip_id="T_ghost").first()
        assert marker is not None
        assert "T_ghost" in rt_mod._recorded_today

    def test_trip_seen_in_rt_today_not_marked(self, obs_db, frozen_now):
        _add_todays_trip(obs_db, "T_seen")
        self._arm_polling()
        rt_mod._seen_in_rt_today.add("T_seen")  # appeared in a feed earlier

        assert rt_mod.record_no_shows(obs_db) == 0

    def test_already_recorded_trip_not_marked(self, obs_db, frozen_now):
        _add_todays_trip(obs_db, "T_done")
        self._arm_polling()
        rt_mod._recorded_today.add("T_done")  # observed/cancelled earlier

        assert rt_mod.record_no_shows(obs_db) == 0

    def test_trip_outside_coverage_window_not_judged(self, obs_db, frozen_now):
        """A trip that departed before polling started could have shown
        evidence we never saw — it must not be marked missed."""
        _add_todays_trip(obs_db, "T_early", dep1="07:00:00", dep2="07:10:00")
        rt_mod._polling_since = _FROZEN_UTC - timedelta(hours=6)  # 09:00 local
        rt_mod._recorded_date = _FROZEN_LOCAL.strftime("%Y%m%d")

        assert rt_mod.record_no_shows(obs_db) == 0

    def test_grace_period_not_elapsed_not_marked(self, obs_db, frozen_now):
        # Final departure 14:45 local; frozen now is 15:00 — inside the
        # 30-minute grace window.
        _add_todays_trip(obs_db, "T_recent", dep1="14:35:00", dep2="14:45:00")
        self._arm_polling()

        assert rt_mod.record_no_shows(obs_db) == 0

    def test_no_polling_coverage_no_sweep(self, obs_db, frozen_now):
        _add_todays_trip(obs_db, "T_ghost")
        rt_mod._polling_since = None  # coverage hole / polling never started

        assert rt_mod.record_no_shows(obs_db) == 0

    def test_sweep_throttled(self, obs_db, frozen_now):
        _add_todays_trip(obs_db, "T_ghost")
        self._arm_polling()
        rt_mod._last_noshow_sweep = _FROZEN_UTC - timedelta(seconds=60)

        assert rt_mod.record_no_shows(obs_db) == 0  # swept < 5 min ago


# ---------------------------------------------------------------------------
# Timezone handling — GTFS times are agency-local (America/Toronto), while
# the process clock is typically UTC.
# ---------------------------------------------------------------------------

class TestTimezoneHandling:
    def test_parse_scheduled_at_is_agency_local(self):
        dt = rt_mod._parse_scheduled_at("15:30:00", "20260710")
        assert dt is not None
        assert dt.tzinfo is not None
        # Wall clock preserved, anchored to the agency zone (EDT on this date).
        assert (dt.hour, dt.minute) == (15, 30)
        assert dt.utcoffset() == datetime(2026, 7, 10, 15, 30, tzinfo=AGENCY_TZ).utcoffset()

    def test_parse_scheduled_at_past_midnight_rolls_wall_clock(self):
        dt = rt_mod._parse_scheduled_at("25:15:00", "20260710")
        assert (dt.day, dt.hour, dt.minute) == (11, 1, 15)
        assert dt.tzinfo is not None

    def test_trip_departing_soon_is_not_recorded_early(self, obs_db):
        """Regression: a trip due 1 h from now (agency-local) must not be
        treated as already departed just because UTC is hours ahead of the
        agency's wall clock (the old code compared the naive schedule time
        directly against UTC)."""
        dep_local = datetime.now(AGENCY_TZ) + timedelta(hours=1)
        obs_db.add(Trip(trip_id="T_soon", route_id="R1",
                        service_id=dep_local.strftime("%Y%m%d"),
                        trip_headsign="GL", direction_id=0))
        obs_db.add(StopTime(trip_id="T_soon", stop_id="S1", stop_sequence=1,
                            departure_time=dep_local.strftime("%H:%M:%S"),
                            arrival_time=dep_local.strftime("%H:%M:%S")))
        obs_db.commit()

        rt_mod.trip_updates["T_soon"] = TripUpdateState(
            trip_id="T_soon", route_id="R1", is_cancelled=False,
            stop_time_overrides={"S1": 60},
        )
        with patch("ingestion.gtfs_realtime.record_observed_departure") as mock_record:
            count = observe_departures(obs_db)

        assert count == 0
        mock_record.assert_not_called()


# ---------------------------------------------------------------------------
# Polling functions (poll_trip_updates, poll_service_alerts,
# poll_vehicle_positions, poll_all)
# All HTTP / feed fetching is patched via _fetch_feed at the module level.
# ---------------------------------------------------------------------------

def _make_trip_feed(trips):
    """Build a minimal mock FeedMessage for poll_trip_updates tests."""
    feed = MagicMock()
    entities = []
    for t in trips:
        entity = MagicMock()
        entity.HasField.return_value = True
        tu = entity.trip_update
        tu.trip.trip_id = t["trip_id"]
        tu.trip.route_id = t.get("route_id", "R1")
        tu.trip.schedule_relationship = 3 if t.get("cancelled") else 0
        stus = []
        for stop_id, delay in t.get("overrides", {}).items():
            stu = MagicMock()
            stu.stop_id = stop_id
            stu.HasField.return_value = True
            stu.departure.delay = delay
            stus.append(stu)
        tu.stop_time_update = stus
        entities.append(entity)
    feed.entity = entities
    return feed


def _make_alert_feed(alerts):
    """Build a minimal mock FeedMessage for poll_service_alerts tests."""
    feed = MagicMock()
    entities = []
    for a in alerts:
        entity = MagicMock()
        entity.id = a.get("id", "A1")
        entity.HasField.return_value = True
        alert = entity.alert
        informed = []
        for route_id in a.get("route_ids", []):
            ie = MagicMock()
            ie.route_id = route_id
            ie.stop_id = ""
            informed.append(ie)
        alert.informed_entity = informed
        if a.get("header"):
            tr = MagicMock()
            tr.text = a["header"]
            alert.header_text.translation = [tr]
        else:
            alert.header_text.translation = []
        alert.description_text.translation = []
        entities.append(entity)
    feed.entity = entities
    return feed


def _make_vehicle_feed(vehicles):
    """Build a minimal mock FeedMessage for poll_vehicle_positions tests."""
    feed = MagicMock()
    entities = []
    for v in vehicles:
        entity = MagicMock()
        entity.HasField.return_value = True
        vp = entity.vehicle
        vp.trip.trip_id = v["trip_id"]
        vp.position.latitude = v.get("lat", 43.6)
        vp.position.longitude = v.get("lon", -79.4)
        vp.timestamp = v.get("timestamp", 0)
        entities.append(entity)
    feed.entity = entities
    return feed


@pytest.fixture(autouse=False)
def reset_poll_state():
    """Reset polling backoff state before/after each polling test."""
    rt_mod._consecutive_poll_failures = 0
    rt_mod._backoff_until = None
    rt_mod._last_fetched = None
    yield
    rt_mod._consecutive_poll_failures = 0
    rt_mod._backoff_until = None


class TestPollTripUpdates:

    @pytest.mark.anyio
    async def test_success_populates_trip_updates(self, reset_poll_state):
        feed = _make_trip_feed([
            {"trip_id": "T1", "route_id": "R1", "overrides": {"S1": 120}},
        ])
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=feed)):
            result = await poll_trip_updates()

        assert result is True
        assert "T1" in rt_mod.trip_updates
        assert rt_mod.trip_updates["T1"].route_id == "R1"
        assert rt_mod.trip_updates["T1"].stop_time_overrides == {"S1": 120}

    @pytest.mark.anyio
    async def test_cancelled_trip_flagged(self, reset_poll_state):
        feed = _make_trip_feed([{"trip_id": "T1", "route_id": "R1", "cancelled": True}])
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=feed)):
            await poll_trip_updates()

        assert rt_mod.trip_updates["T1"].is_cancelled is True

    @pytest.mark.anyio
    async def test_fetch_failure_returns_false(self, reset_poll_state):
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=None)):
            result = await poll_trip_updates()

        assert result is False

    @pytest.mark.anyio
    async def test_clears_previous_state(self, reset_poll_state):
        rt_mod.trip_updates["OLD"] = TripUpdateState(trip_id="OLD", route_id="R1")
        feed = _make_trip_feed([{"trip_id": "NEW", "route_id": "R1"}])
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=feed)):
            await poll_trip_updates()

        assert "OLD" not in rt_mod.trip_updates
        assert "NEW" in rt_mod.trip_updates


class TestPollServiceAlerts:

    @pytest.mark.anyio
    async def test_success_populates_service_alerts(self, reset_poll_state):
        feed = _make_alert_feed([
            {"id": "A1", "route_ids": ["R1"], "header": "Delay on R1"},
        ])
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=feed)):
            result = await poll_service_alerts()

        assert result is True
        assert len(rt_mod.service_alerts) == 1
        assert rt_mod.service_alerts[0].alert_id == "A1"
        assert "R1" in rt_mod.service_alerts[0].affected_route_ids
        assert rt_mod.service_alerts[0].header == "Delay on R1"

    @pytest.mark.anyio
    async def test_fetch_failure_returns_false(self, reset_poll_state):
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=None)):
            result = await poll_service_alerts()

        assert result is False

    @pytest.mark.anyio
    async def test_clears_previous_alerts(self, reset_poll_state):
        from ingestion.gtfs_realtime import ServiceAlertState
        rt_mod.service_alerts.append(
            ServiceAlertState(alert_id="OLD", header="Old", description="",
                              affected_route_ids=[])
        )
        feed = _make_alert_feed([{"id": "NEW", "route_ids": []}])
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=feed)):
            await poll_service_alerts()

        alert_ids = [a.alert_id for a in rt_mod.service_alerts]
        assert "OLD" not in alert_ids
        assert "NEW" in alert_ids


class TestPollVehiclePositions:

    @pytest.mark.anyio
    async def test_success_populates_vehicle_positions(self, reset_poll_state):
        feed = _make_vehicle_feed([
            {"trip_id": "T1", "lat": 43.65, "lon": -79.38, "timestamp": 1234},
        ])
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=feed)):
            result = await poll_vehicle_positions()

        assert result is True
        assert "T1" in rt_mod.vehicle_positions
        assert rt_mod.vehicle_positions["T1"]["lat"] == pytest.approx(43.65)
        assert rt_mod.vehicle_positions["T1"]["timestamp"] == 1234

    @pytest.mark.anyio
    async def test_fetch_failure_returns_false(self, reset_poll_state):
        with patch("ingestion.gtfs_realtime._fetch_feed", new=AsyncMock(return_value=None)):
            result = await poll_vehicle_positions()

        assert result is False


class TestPollAll:

    @pytest.mark.anyio
    async def test_skips_when_no_api_key(self, reset_poll_state):
        with patch.object(rt_mod, "GTFS_RT_API_KEY", ""):
            with patch("ingestion.gtfs_realtime.poll_trip_updates",
                       new=AsyncMock()) as mock_poll:
                await poll_all()
                mock_poll.assert_not_called()

    @pytest.mark.anyio
    async def test_skips_during_backoff(self, reset_poll_state):
        rt_mod._backoff_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        with patch.object(rt_mod, "GTFS_RT_API_KEY", "test-key"):
            with patch("ingestion.gtfs_realtime.poll_trip_updates",
                       new=AsyncMock()) as mock_poll:
                await poll_all()
                mock_poll.assert_not_called()

    @pytest.mark.anyio
    async def test_all_fail_increments_backoff_counter(self, reset_poll_state):
        with patch.object(rt_mod, "GTFS_RT_API_KEY", "test-key"):
            with patch("ingestion.gtfs_realtime.poll_trip_updates",
                       new=AsyncMock(return_value=False)), \
                 patch("ingestion.gtfs_realtime.poll_service_alerts",
                       new=AsyncMock(return_value=False)), \
                 patch("ingestion.gtfs_realtime.poll_vehicle_positions",
                       new=AsyncMock(return_value=False)):
                await poll_all()

        assert rt_mod._consecutive_poll_failures == 1
        assert rt_mod._backoff_until is not None
        assert rt_mod._backoff_until > datetime.now(timezone.utc)

    @pytest.mark.anyio
    async def test_partial_success_resets_backoff(self, reset_poll_state):
        rt_mod._consecutive_poll_failures = 3
        with patch.object(rt_mod, "GTFS_RT_API_KEY", "test-key"):
            with patch("ingestion.gtfs_realtime.poll_trip_updates",
                       new=AsyncMock(return_value=True)), \
                 patch("ingestion.gtfs_realtime.poll_service_alerts",
                       new=AsyncMock(return_value=False)), \
                 patch("ingestion.gtfs_realtime.poll_vehicle_positions",
                       new=AsyncMock(return_value=False)):
                await poll_all()

        assert rt_mod._consecutive_poll_failures == 0
        assert rt_mod._backoff_until is None

    @pytest.mark.anyio
    async def test_backoff_doubles_on_consecutive_failures(self, reset_poll_state):
        with patch.object(rt_mod, "GTFS_RT_API_KEY", "test-key"):
            with patch("ingestion.gtfs_realtime.poll_trip_updates",
                       new=AsyncMock(return_value=False)), \
                 patch("ingestion.gtfs_realtime.poll_service_alerts",
                       new=AsyncMock(return_value=False)), \
                 patch("ingestion.gtfs_realtime.poll_vehicle_positions",
                       new=AsyncMock(return_value=False)):
                await poll_all()  # failure #1 → 60s backoff
                backoff_1 = rt_mod._backoff_until
                rt_mod._backoff_until = None  # reset so next call isn't skipped

                await poll_all()  # failure #2 → 120s backoff
                backoff_2 = rt_mod._backoff_until

        # Second backoff should be further in the future than first
        assert backoff_2 > backoff_1
