"""
Integration tests for API endpoints.

The FastAPI lifespan (init_db, build_graph, scheduler) is patched out
for every test.  Each test gets its own in-memory SQLite database via
the db_session / client fixtures, so tests are fully isolated.
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, Stop
from db.session import get_session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """Fresh in-memory SQLite database, schema pre-created, per test.

    StaticPool is required so that create_all and the session both use
    the same single connection — otherwise each pool checkout gets a new
    in-memory DB that has no tables.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(db_session):
    """
    TestClient with:
      - lifespan init_db / build_graph / scheduler patched to no-ops
      - get_session dependency overridden to use the test db_session
    """
    from api.main import app

    def override_get_session():
        yield db_session

    with (
        patch("api.main.init_db"),
        patch("api.main.build_graph"),
        patch("api.main.SessionLocal", return_value=MagicMock()),
    ):
        app.dependency_overrides[get_session] = override_get_session
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_contains_status_ok(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"

    def test_contains_timestamp(self, client):
        body = client.get("/health").json()
        assert "timestamp" in body

    def test_gtfs_section_present(self, client):
        body = client.get("/health").json()
        gtfs = body["gtfs"]
        assert "stops" in gtfs
        assert "trips" in gtfs
        assert "graph_nodes" in gtfs
        assert "graph_edges" in gtfs
        assert "graph_built" in gtfs
        assert "last_built_at" in gtfs
        assert "latest_service_date" in gtfs
        assert "next_refresh_at" in gtfs

    def test_reliability_section_present(self, client):
        body = client.get("/health").json()
        rel = body["reliability"]
        assert "records" in rel
        assert "last_seeded_at" in rel

    def test_gtfs_rt_section_present(self, client):
        body = client.get("/health").json()
        assert "polling_active" in body["gtfs_rt"]

    def test_empty_db_returns_zero_counts(self, client):
        body = client.get("/health").json()
        assert body["gtfs"]["stops"] == 0
        assert body["gtfs"]["trips"] == 0
        assert body["gtfs"]["latest_service_date"] is None
        assert body["reliability"]["records"] == 0
        assert body["reliability"]["last_seeded_at"] is None

    def test_graph_not_built_reports_false(self, client):
        # build_graph is patched to a no-op in the client fixture, so
        # the module-level graph cache is never set → graph_built should be False
        with patch("api.main.get_graph", side_effect=RuntimeError("not built")):
            body = client.get("/health").json()
        assert body["gtfs"]["graph_built"] is False
        assert body["gtfs"]["graph_nodes"] == 0


# ---------------------------------------------------------------------------
# GET /stops
# ---------------------------------------------------------------------------

class TestStopsSearch:
    def test_empty_db_returns_empty_list(self, client):
        resp = client.get("/stops?query=Guelph")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_matching_stop_returned(self, client, db_session):
        db_session.add(
            Stop(stop_id="GL", stop_name="Guelph Central GO",
                 stop_lat=43.5448, stop_lon=-80.2482)
        )
        db_session.commit()

        resp = client.get("/stops?query=Guelph")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert results[0]["stop_id"] == "GL"
        assert "Guelph" in results[0]["stop_name"]

    def test_case_insensitive_match(self, client, db_session):
        db_session.add(
            Stop(stop_id="UN", stop_name="Union Station GO",
                 stop_lat=43.6453, stop_lon=-79.3806)
        )
        db_session.commit()

        resp = client.get("/stops?query=union")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_no_match_returns_empty(self, client, db_session):
        db_session.add(
            Stop(stop_id="UN", stop_name="Union Station GO",
                 stop_lat=43.6453, stop_lon=-79.3806)
        )
        db_session.commit()

        resp = client.get("/stops?query=Kitchener")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_query_too_short_returns_422(self, client):
        resp = client.get("/stops?query=G")
        assert resp.status_code == 422

    def test_missing_query_param_returns_422(self, client):
        resp = client.get("/stops")
        assert resp.status_code == 422

    def test_response_shape(self, client, db_session):
        db_session.add(
            Stop(stop_id="GL", stop_name="Guelph Central GO",
                 stop_lat=43.5448, stop_lon=-80.2482)
        )
        db_session.commit()

        result = client.get("/stops?query=Guelph").json()[0]
        assert set(result.keys()) == {"stop_id", "stop_name", "lat", "lon"}


# ---------------------------------------------------------------------------
# GET /routes
# ---------------------------------------------------------------------------

_FAKE_ROUTE = [
    {
        "kind": "trip",
        "from_stop_id": "UN",
        "to_stop_id": "GL",
        "from_stop_name": "Union Station GO",
        "to_stop_name": "Guelph Central GO",
        "trip_id": "T1",
        "route_id": "GT1",
        "service_id": "20260211",
        "departure_time": "08:00:00",
        "arrival_time": "09:21:00",
        "travel_seconds": 4860,
    }
]

_FAKE_LIVE_RISK = {
    "risk_score": 0.2,
    "risk_label": "Low",
    "modifiers": [],
    "is_cancelled": False,
}


class TestGetRoutes:
    # --- parameter validation ---

    def test_missing_origin_returns_422(self, client):
        resp = client.get("/routes?destination=GL")
        assert resp.status_code == 422

    def test_missing_destination_returns_422(self, client):
        resp = client.get("/routes?origin=UN")
        assert resp.status_code == 422

    def test_invalid_departure_time_returns_422(self, client):
        resp = client.get(
            "/routes?origin=UN&destination=GL"
            "&travel_date=2026-02-11&departure_time=notATime"
        )
        assert resp.status_code == 422

    def test_invalid_travel_date_returns_422(self, client):
        resp = client.get(
            "/routes?origin=UN&destination=GL&travel_date=not-a-date"
        )
        assert resp.status_code == 422

    # --- routing errors ---

    def test_unknown_stop_returns_404(self, client):
        with patch("api.main.find_routes",
                   side_effect=ValueError("Origin stop 'ZZ' not found in graph.")):
            resp = client.get(
                "/routes?origin=ZZ&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 404
        assert "ZZ" in resp.json()["detail"]

    def test_no_routes_found_returns_404(self, client):
        with patch("api.main.find_routes", return_value=[]):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 404

    # --- valid response ---

    def test_valid_route_returns_200(self, client):
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability", return_value=0.8),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 200

    def test_response_contains_routes_key(self, client):
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability", return_value=0.8),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            body = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            ).json()

        assert "routes" in body
        assert len(body["routes"]) == 1

    def test_route_has_expected_fields(self, client):
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability", return_value=0.8),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            route = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            ).json()["routes"][0]

        assert "legs" in route
        assert "total_travel_seconds" in route
        assert "risk_score" in route
        assert "risk_label" in route

    def test_total_travel_seconds_correct(self, client):
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability", return_value=0.8),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            route = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            ).json()["routes"][0]

        assert route["total_travel_seconds"] == 4860

    def test_risk_score_and_label_present(self, client):
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability", return_value=0.8),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            route = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            ).json()["routes"][0]

        assert route["risk_label"] == "Low"
        assert route["risk_score"] == pytest.approx(0.2, abs=0.01)

    def test_hhmm_departure_time_accepted(self, client):
        """HH:MM (without seconds) should be accepted."""
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability", return_value=0.8),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /ingest/gtfs-static — auth
# ---------------------------------------------------------------------------

class TestIngestAuth:
    """
    The ingest endpoint is open when INGEST_API_KEY is unset (local dev)
    and requires a matching X-API-Key header when it is set.
    """

    def test_open_when_no_key_configured(self, client):
        """No INGEST_API_KEY set → request succeeds without a header."""
        with (
            patch("api.main.INGEST_API_KEY", ""),
            patch("api.main.refresh_static_data"),
            patch("api.main.build_graph"),
            patch("api.main.seed_from_static", return_value=0),
        ):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 200

    def test_correct_key_accepted(self, client):
        """Correct X-API-Key header → 200."""
        with (
            patch("api.main.INGEST_API_KEY", "secret"),
            patch("api.main.refresh_static_data"),
            patch("api.main.build_graph"),
            patch("api.main.seed_from_static", return_value=0),
        ):
            resp = client.post(
                "/ingest/gtfs-static",
                headers={"X-API-Key": "secret"},
            )
        assert resp.status_code == 200

    def test_wrong_key_rejected(self, client):
        """Wrong X-API-Key header → 401."""
        with (
            patch("api.main.INGEST_API_KEY", "secret"),
            patch("api.main.refresh_static_data"),
            patch("api.main.build_graph"),
        ):
            resp = client.post(
                "/ingest/gtfs-static",
                headers={"X-API-Key": "wrong"},
            )
        assert resp.status_code == 401

    def test_missing_header_rejected(self, client):
        """No X-API-Key header when key is configured → 401."""
        with (
            patch("api.main.INGEST_API_KEY", "secret"),
            patch("api.main.refresh_static_data"),
            patch("api.main.build_graph"),
        ):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 401

    def test_reseed_chained_after_ingest(self, client):
        """POST /ingest/gtfs-static calls seed_from_static with fill_gaps_only=False."""
        with (
            patch("api.main.INGEST_API_KEY", ""),
            patch("api.main.refresh_static_data"),
            patch("api.main.build_graph"),
            patch("api.main.seed_from_static", return_value=42) as mock_seed,
        ):
            resp = client.post("/ingest/gtfs-static")

        assert resp.status_code == 200
        mock_seed.assert_called_once()
        _, kwargs = mock_seed.call_args
        assert kwargs.get("fill_gaps_only") is False

    def test_ingest_response_includes_seed_count(self, client):
        """Response body reports how many reliability records were reseeded."""
        with (
            patch("api.main.INGEST_API_KEY", ""),
            patch("api.main.refresh_static_data"),
            patch("api.main.build_graph"),
            patch("api.main.seed_from_static", return_value=99),
        ):
            body = client.post("/ingest/gtfs-static").json()

        assert "99" in body["message"]


# ---------------------------------------------------------------------------
# _daily_gtfs_refresh job function
# ---------------------------------------------------------------------------

class TestDailyGtfsRefreshJob:

    @pytest.mark.anyio
    async def test_calls_refresh_build_seed(self):
        """Job invokes refresh_static_data, build_graph, and seed_from_static."""
        from api.main import _daily_gtfs_refresh

        mock_session = MagicMock()
        with (
            patch("api.main.SessionLocal", return_value=mock_session),
            patch("api.main.refresh_static_data", new_callable=AsyncMock) as mock_refresh,
            patch("api.main.build_graph") as mock_build,
            patch("api.main.seed_from_static", return_value=5) as mock_seed,
        ):
            await _daily_gtfs_refresh()

        mock_refresh.assert_called_once_with(mock_session)
        mock_build.assert_called_once_with(mock_session)
        mock_seed.assert_called_once_with(mock_session, fill_gaps_only=False)

    @pytest.mark.anyio
    async def test_error_does_not_propagate(self):
        """A failure during refresh is swallowed — the job must not crash the scheduler."""
        from api.main import _daily_gtfs_refresh

        with (
            patch("api.main.SessionLocal", return_value=MagicMock()),
            patch("api.main.refresh_static_data", new_callable=AsyncMock,
                  side_effect=Exception("network down")),
            patch("api.main.build_graph"),
            patch("api.main.seed_from_static"),
        ):
            await _daily_gtfs_refresh()  # must not raise

    @pytest.mark.anyio
    async def test_session_always_closed(self):
        """DB session is closed in the finally block even when the job fails."""
        from api.main import _daily_gtfs_refresh

        mock_session = MagicMock()
        with (
            patch("api.main.SessionLocal", return_value=mock_session),
            patch("api.main.refresh_static_data", new_callable=AsyncMock,
                  side_effect=Exception("fail")),
            patch("api.main.build_graph"),
            patch("api.main.seed_from_static"),
        ):
            await _daily_gtfs_refresh()

        mock_session.close.assert_called_once()
