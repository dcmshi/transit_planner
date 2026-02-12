"""
Tests for ingestion.seed_reliability.seed_from_static().

Uses a minimal in-memory SQLite DB seeded with a few trips and stop_times
so we can verify the aggregation and upsert logic without needing the full
904-stop GO Transit dataset.
"""

import pytest
from datetime import date, datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, ReliabilityRecord, Stop, StopTime, Trip
from ingestion.seed_reliability import _PRIORS, seed_from_static


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    """In-memory SQLite DB with schema, yielding a session."""
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


def _add_trip(session, trip_id, route_id, service_id):
    session.add(Trip(
        trip_id=trip_id,
        route_id=route_id,
        service_id=service_id,
        trip_headsign="Test",
        direction_id=0,
    ))


def _add_stop_time(session, trip_id, stop_id, seq, dep, arr="00:00:00"):
    session.add(StopTime(
        trip_id=trip_id,
        stop_id=stop_id,
        stop_sequence=seq,
        departure_time=dep,
        arrival_time=arr,
    ))


# ---------------------------------------------------------------------------
# seed_from_static unit tests
# ---------------------------------------------------------------------------

class TestSeedFromStatic:

    def test_raises_when_no_trips(self, db):
        with pytest.raises(RuntimeError, match="No trips in database"):
            seed_from_static(db, window_days=7)

    def test_stale_feed_falls_back_to_feed_dates(self, db):
        """When today is past all service dates, falls back to feed date range."""
        _add_trip(db, "T1", "R1", "20200101")  # stale feed date
        _add_stop_time(db, "T1", "S1", 1, "08:00:00")
        db.commit()

        # today is far in the future — the seeder should use the feed's own dates
        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2030, 1, 1)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            result = seed_from_static(db, window_days=7)

        # Falls back to feed data — still writes a record
        assert result == 1

    def test_creates_record_for_weekday_am_peak(self, db):
        """A trip departing 08:00 on a Monday creates a weekday_am_peak record."""
        service_id = "20260209"  # Monday 2026-02-09
        _add_trip(db, "T1", "R1", service_id)
        _add_stop_time(db, "T1", "S1", 1, "08:00:00")
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 9)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            written = seed_from_static(db, window_days=7)

        assert written == 1
        record = db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak"
        ).first()
        assert record is not None
        assert record.scheduled_departures == 1

    def test_synthetic_rates_applied(self, db):
        """observed_departures and cancellation_count match the prior rates."""
        service_id = "20260209"  # Monday
        # Add 10 trips on the same route/stop to make rounding visible
        for i in range(10):
            _add_trip(db, f"T{i}", "R1", service_id)
            _add_stop_time(db, f"T{i}", "S1", 1, "08:00:00")
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 9)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            seed_from_static(db, window_days=1)

        record = db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak"
        ).first()
        prior = _PRIORS["weekday_am_peak"]
        assert record.observed_departures == round(10 * prior["reliability_rate"])
        assert record.cancellation_count == round(10 * prior["cancellation_rate"])

    def test_buckets_separated_correctly(self, db):
        """AM peak and PM peak trips on the same day create separate records."""
        service_id = "20260209"  # Monday
        _add_trip(db, "T_am", "R1", service_id)
        _add_stop_time(db, "T_am", "S1", 1, "08:00:00")  # am_peak
        _add_trip(db, "T_pm", "R1", service_id)
        _add_stop_time(db, "T_pm", "S1", 2, "16:00:00")  # pm_peak
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 9)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            written = seed_from_static(db, window_days=1)

        assert written == 2
        buckets = {
            r.time_bucket
            for r in db.query(ReliabilityRecord).filter_by(route_id="R1", stop_id="S1").all()
        }
        assert buckets == {"weekday_am_peak", "weekday_pm_peak"}

    def test_weekend_bucket_assigned(self, db):
        """A Saturday departure is classified as weekend."""
        service_id = "20260207"  # Saturday
        _add_trip(db, "T1", "R1", service_id)
        _add_stop_time(db, "T1", "S1", 1, "10:00:00")
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 7)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            seed_from_static(db, window_days=1)

        record = db.query(ReliabilityRecord).filter_by(
            route_id="R1", stop_id="S1", time_bucket="weekend"
        ).first()
        assert record is not None

    def test_idempotent_rerun(self, db):
        """Calling seed twice produces the same record count (upsert, not append)."""
        service_id = "20260209"
        _add_trip(db, "T1", "R1", service_id)
        _add_stop_time(db, "T1", "S1", 1, "08:00:00")
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 9)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            first = seed_from_static(db, window_days=1)
            second = seed_from_static(db, window_days=1)

        assert first == second == 1
        assert db.query(ReliabilityRecord).count() == 1

    def test_multiple_routes_and_stops(self, db):
        """Two routes, two stops, one time bucket → 4 records."""
        service_id = "20260209"
        for route in ("R1", "R2"):
            for stop in ("S1", "S2"):
                trip_id = f"T_{route}_{stop}"
                _add_trip(db, trip_id, route, service_id)
                _add_stop_time(db, trip_id, stop, 1, "08:00:00")
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 9)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            written = seed_from_static(db, window_days=1)

        assert written == 4

    def test_window_date_recorded(self, db):
        """window_start_date and window_end_date are stored on the record."""
        service_id = "20260209"
        _add_trip(db, "T1", "R1", service_id)
        _add_stop_time(db, "T1", "S1", 1, "08:00:00")
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 9)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            seed_from_static(db, window_days=1)

        record = db.query(ReliabilityRecord).first()
        assert record.window_start_date == "20260209"
        assert record.window_end_date == "20260209"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestReliabilitySeedEndpoint:

    @pytest.fixture
    def client(self, db):
        from api.main import app
        from db.session import get_session
        from fastapi.testclient import TestClient
        from unittest.mock import MagicMock

        def override_session():
            yield db

        with (
            patch("api.main.init_db"),
            patch("api.main.build_graph"),
            patch("api.main.SessionLocal", return_value=MagicMock()),
        ):
            app.dependency_overrides[get_session] = override_session
            with TestClient(app) as c:
                yield c
            app.dependency_overrides.clear()

    def test_no_gtfs_data_returns_409(self, client):
        """Empty DB → RuntimeError → 409 Conflict."""
        resp = client.post("/ingest/reliability-seed")
        assert resp.status_code == 409

    def test_with_data_returns_200(self, client, db):
        """Pre-seeded trip → 200 with records_written > 0."""
        service_id = "20260209"
        _add_trip(db, "T1", "R1", service_id)
        _add_stop_time(db, "T1", "S1", 1, "08:00:00")
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 9)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            resp = client.post("/ingest/reliability-seed?window_days=1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["records_written"] == 1

    def test_window_days_param_passed_through(self, client, db):
        """?window_days query param is accepted."""
        service_id = "20260209"
        _add_trip(db, "T1", "R1", service_id)
        _add_stop_time(db, "T1", "S1", 1, "08:00:00")
        db.commit()

        with patch("ingestion.seed_reliability.date") as mock_date:
            mock_date.today.return_value = date(2026, 2, 9)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            resp = client.post("/ingest/reliability-seed?window_days=7")

        assert resp.status_code == 200

    def test_invalid_window_days_returns_422(self, client):
        resp = client.post("/ingest/reliability-seed?window_days=0")
        assert resp.status_code == 422
