"""
Integration tests for API endpoints.

The FastAPI lifespan (init_db, build_graph, scheduler) is patched out
for every test.  Each test gets its own in-memory SQLite database via
the db_session / client fixtures, so tests are fully isolated.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
        # Belt and braces on top of conftest's GTFS_RT_API_KEY="" pin: the
        # lifespan must never fire real RT polls from unit tests.
        patch("api.main.GTFS_RT_API_KEY", ""),
    ):
        app.dependency_overrides[get_session] = override_get_session
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_route_cache():
    """The route cache is module-level in api.main — with negative caching,
    one test's empty result would otherwise poison the next test's query
    for the same origin/destination/time."""
    from api.main import _clear_routes_cache
    _clear_routes_cache()
    yield


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

    def test_gtfs_rt_freshness_fields_present(self, client):
        """Operators need feed health, not just a polling flag."""
        rt = client.get("/health").json()["gtfs_rt"]
        assert "last_fetched_at" in rt
        assert "consecutive_failures" in rt
        assert "backing_off_until" in rt
        assert "polling_coverage_since" in rt
        assert rt["trip_updates"] == 0  # nothing polled in tests
        assert rt["consecutive_failures"] == 0

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

    def test_query_too_long_returns_422(self, client):
        resp = client.get(f"/stops?query={'x' * 129}")
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
        assert set(result.keys()) == {"stop_id", "stop_name", "lat", "lon", "routes_served"}


# ---------------------------------------------------------------------------
# GET /alerts
# ---------------------------------------------------------------------------

class TestAlerts:
    @pytest.fixture(autouse=True)
    def _clean_rt_state(self):
        from ingestion.mock_realtime import clear_all
        clear_all()
        yield
        clear_all()

    def test_empty_when_no_alerts(self, client):
        resp = client.get("/alerts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_active_alerts(self, client):
        from ingestion.mock_realtime import inject_alert

        inject_alert(
            "A1", "Detour on Route 27", "Construction at Hwy 7",
            route_ids=["R27"], stop_ids=["S1"],
        )
        body = client.get("/alerts").json()

        assert len(body) == 1
        assert body[0]["alert_id"] == "A1"
        assert body[0]["header"] == "Detour on Route 27"
        assert body[0]["affected_route_ids"] == ["R27"]
        assert body[0]["affected_stop_ids"] == ["S1"]
        assert body[0]["fetched_at"]  # ISO timestamp present


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimit:
    @pytest.fixture(autouse=True)
    def _clean_buckets(self):
        import api.main as main_mod
        main_mod._rate_buckets.clear()
        yield
        main_mod._rate_buckets.clear()

    def test_requests_over_limit_get_429(self, client):
        with patch("api.main.RATE_LIMIT_PER_MINUTE", 3):
            statuses = [
                client.get("/stops?query=Guelph").status_code for _ in range(4)
            ]
        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429

    def test_limit_disabled_when_zero(self, client):
        with patch("api.main.RATE_LIMIT_PER_MINUTE", 0):
            statuses = [
                client.get("/stops?query=Guelph").status_code for _ in range(5)
            ]
        assert statuses == [200] * 5

    def test_health_not_rate_limited(self, client):
        with patch("api.main.RATE_LIMIT_PER_MINUTE", 1):
            client.get("/stops?query=Guelph")  # consume the budget
            assert client.get("/health").status_code == 200

    def test_429_includes_retry_after(self, client):
        with patch("api.main.RATE_LIMIT_PER_MINUTE", 1):
            client.get("/stops?query=Guelph")
            resp = client.get("/stops?query=Guelph")
        assert resp.status_code == 429
        assert 1 <= int(resp.headers["Retry-After"]) <= 61

    def test_stale_idle_buckets_evicted(self, client):
        """Regression: buckets are never emptied by their own IP's absence,
        so eviction must key on the age of the newest entry — the old
        'delete empty buckets' cleanup could never delete anything."""
        import time as time_mod
        from collections import deque

        import api.main as main_mod

        stale_ts = time_mod.monotonic() - 3600  # far outside the window
        for i in range(5):
            main_mod._rate_buckets[f"10.0.0.{i}"] = deque([stale_ts])

        with (
            patch("api.main.RATE_LIMIT_PER_MINUTE", 100),
            patch("api.main._RATE_BUCKETS_MAX", 3),  # force the cleanup pass
        ):
            client.get("/stops?query=Guelph")

        assert not any(k.startswith("10.0.0.") for k in main_mod._rate_buckets)


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
            patch("api.main.get_historical_reliability_batch", return_value={}),
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
            patch("api.main.get_historical_reliability_batch", return_value={}),
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
            patch("api.main.get_historical_reliability_batch", return_value={}),
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
            patch("api.main.get_historical_reliability_batch", return_value={}),
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
            patch("api.main.get_historical_reliability_batch", return_value={}),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            route = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            ).json()["routes"][0]

        assert route["risk_label"] == "Low"
        assert route["risk_score"] == pytest.approx(0.2, abs=0.01)

    def test_historical_bucket_uses_leg_departure_not_query_time(self, client):
        """Regression: the historical-reliability bucket must come from the
        leg's scheduled departure on the travel date (a 08:00 weekday leg →
        weekday_am_peak), not from the wall clock at query time."""
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability_batch", return_value={}) as mock_hist,
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK) as mock_live,
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"  # Wednesday
            )

        assert resp.status_code == 200
        # _FAKE_ROUTE departs 08:00:00 on 2026-02-11 → weekday_am_peak,
        # regardless of when this test happens to run.
        batch_keys = mock_hist.call_args.args[0]
        assert batch_keys == [("GT1", "UN", "weekday_am_peak")]
        assert mock_live.call_args.kwargs["scheduled_dt"] == datetime(2026, 2, 11, 8, 0, 0)

    def test_live_delay_adds_expected_times_same_day(self, client):
        from datetime import datetime as _dt

        from config import AGENCY_TZ
        today = _dt.now(AGENCY_TZ).strftime("%Y-%m-%d")
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability_batch", return_value={}),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
            patch("api.main.get_live_delay", return_value=300),
        ):
            leg = client.get(
                f"/routes?origin=UN&destination=GL"
                f"&travel_date={today}&departure_time=08:00"
            ).json()["routes"][0]["legs"][0]

        assert leg["live_delay_seconds"] == 300
        assert leg["expected_departure"] == "08:05:00"  # 08:00 + 5 min
        assert leg["expected_arrival"] == "09:26:00"    # 09:21 + 5 min

    def test_no_expected_times_on_future_dates(self, client):
        """Regression: trip_ids repeat across service days — today's live
        delay must not produce expected times for a future travel date."""
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability_batch", return_value={}),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
            patch("api.main.get_live_delay", return_value=300),
        ):
            leg = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2099-02-11&departure_time=08:00"
            ).json()["routes"][0]["legs"][0]

        assert leg["live_delay_seconds"] is None
        assert leg["expected_departure"] is None
        assert leg["expected_arrival"] is None

    def test_hhmm_departure_time_accepted(self, client):
        """HH:MM (without seconds) should be accepted."""
        with (
            patch("api.main.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.main.get_historical_reliability_batch", return_value={}),
            patch("api.main.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 200

    def test_out_of_range_hour_returns_422(self, client):
        """departure_time with hour > 23 should return 422."""
        resp = client.get(
            "/routes?origin=UN&destination=GL"
            "&travel_date=2026-02-11&departure_time=25:00"
        )
        assert resp.status_code == 422

    def test_out_of_range_minute_returns_422(self, client):
        """departure_time with minute > 59 should return 422."""
        resp = client.get(
            "/routes?origin=UN&destination=GL"
            "&travel_date=2026-02-11&departure_time=08:99"
        )
        assert resp.status_code == 422

    def test_origin_equals_destination_returns_422(self, client):
        """Same stop for origin and destination should return 422 before routing."""
        resp = client.get(
            "/routes?origin=UN&destination=UN"
            "&travel_date=2026-02-11&departure_time=08:00"
        )
        assert resp.status_code == 422
        assert "different" in resp.json()["detail"].lower()

    def test_unexpected_routing_exception_returns_500(self, client):
        """A non-ValueError exception from find_routes should return 500."""
        import api.main as main_mod
        main_mod._routes_cache.clear()
        with patch("api.main.find_routes", side_effect=RuntimeError("graph exploded")):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /ingest/gtfs-static — auth
# ---------------------------------------------------------------------------

@pytest.fixture
def _reset_ingest_state():
    """Reset the module-level ingest slot before and after a test."""
    import api.main as main_mod

    def _reset():
        main_mod._ingest_state.update(
            running=False, started_at=None, finished_at=None,
            last_status=None, last_message=None,
        )

    _reset()
    yield
    _reset()


class TestIngestAuth:
    """
    The ingest endpoint is open when INGEST_API_KEY is unset (local dev)
    and requires a matching X-API-Key header when it is set.
    The actual work runs in the background — auth tests stub it out.
    """

    @pytest.fixture(autouse=True)
    def _state(self, _reset_ingest_state):
        yield

    def test_open_when_no_key_configured(self, client):
        """No INGEST_API_KEY set → request accepted without a header."""
        with (
            patch("api.main.INGEST_API_KEY", ""),
            patch("api.main._run_gtfs_ingest", new_callable=AsyncMock),
        ):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 202

    def test_correct_key_accepted(self, client):
        """Correct X-API-Key header → 202."""
        with (
            patch("api.main.INGEST_API_KEY", "secret"),
            patch("api.main._run_gtfs_ingest", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/ingest/gtfs-static",
                headers={"X-API-Key": "secret"},
            )
        assert resp.status_code == 202

    def test_wrong_key_rejected(self, client):
        """Wrong X-API-Key header → 401."""
        with patch("api.main.INGEST_API_KEY", "secret"):
            resp = client.post(
                "/ingest/gtfs-static",
                headers={"X-API-Key": "wrong"},
            )
        assert resp.status_code == 401

    def test_missing_header_rejected(self, client):
        """No X-API-Key header when key is configured → 401."""
        with patch("api.main.INGEST_API_KEY", "secret"):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 401


class TestIngestBackground:
    """202 semantics, the single-slot guard, and the status endpoint."""

    @pytest.fixture(autouse=True)
    def _state(self, _reset_ingest_state):
        yield

    def test_returns_202_accepted(self, client):
        with (
            patch("api.main.INGEST_API_KEY", ""),
            patch("api.main._run_gtfs_ingest", new_callable=AsyncMock),
        ):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_concurrent_ingest_rejected_409(self, client):
        import api.main as main_mod
        main_mod._ingest_state["running"] = True
        with patch("api.main.INGEST_API_KEY", ""):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 409

    def test_status_endpoint_reports_state(self, client):
        import api.main as main_mod
        main_mod._ingest_state.update(
            running=False, started_at="2026-07-10T12:00:00+00:00",
            finished_at="2026-07-10T12:01:00+00:00",
            last_status="ok", last_message="done",
        )
        with patch("api.main.INGEST_API_KEY", ""):
            body = client.get("/ingest/status").json()
        assert body["running"] is False
        assert body["last_status"] == "ok"
        assert body["last_message"] == "done"

    @pytest.mark.anyio
    async def test_run_ingest_chains_refresh_build_seed(self):
        """The background body chains refresh → build → full reseed and
        records success in the ingest state."""
        import api.main as main_mod
        from api.main import _run_gtfs_ingest

        mock_session = MagicMock()
        main_mod._ingest_state["running"] = True  # slot claimed by endpoint
        with (
            patch("api.main.SessionLocal", return_value=mock_session),
            patch("api.main.refresh_static_data", new_callable=AsyncMock) as mock_refresh,
            patch("api.main.build_graph") as mock_build,
            patch("api.main.seed_from_static", return_value=42) as mock_seed,
        ):
            await _run_gtfs_ingest()

        mock_refresh.assert_called_once_with(mock_session)
        mock_build.assert_called_once_with(mock_session)
        _, kwargs = mock_seed.call_args
        assert kwargs.get("fill_gaps_only") is False
        assert main_mod._ingest_state["running"] is False
        assert main_mod._ingest_state["last_status"] == "ok"
        assert "42" in main_mod._ingest_state["last_message"]
        mock_session.close.assert_called_once()

    @pytest.mark.anyio
    async def test_cancelled_ingest_releases_slot(self):
        """Regression: CancelledError bypasses `except Exception`; a
        cancelled ingest task must not leave running=True forever (which
        would 409 every manual ingest and skip every daily refresh)."""
        import asyncio

        import api.main as main_mod
        from api.main import _run_gtfs_ingest

        main_mod._ingest_state["running"] = True

        async def hang(session):
            await asyncio.Event().wait()

        with (
            patch("api.main.SessionLocal", return_value=MagicMock()),
            patch("api.main.refresh_static_data", side_effect=hang),
        ):
            task = asyncio.get_running_loop().create_task(_run_gtfs_ingest())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert main_mod._ingest_state["running"] is False
        assert main_mod._ingest_state["last_status"] == "error"
        assert "cancelled" in main_mod._ingest_state["last_message"].lower()

    @pytest.mark.anyio
    async def test_run_ingest_records_error(self):
        import api.main as main_mod
        from api.main import _run_gtfs_ingest

        main_mod._ingest_state["running"] = True
        with (
            patch("api.main.SessionLocal", return_value=MagicMock()),
            patch("api.main.refresh_static_data", new_callable=AsyncMock,
                  side_effect=Exception("feed down")),
        ):
            await _run_gtfs_ingest()  # must not raise

        assert main_mod._ingest_state["running"] is False
        assert main_mod._ingest_state["last_status"] == "error"
        assert "feed down" in main_mod._ingest_state["last_message"]

# ---------------------------------------------------------------------------
# _daily_gtfs_refresh job function
# ---------------------------------------------------------------------------

class TestDailyGtfsRefreshJob:

    @pytest.fixture(autouse=True)
    def _state(self, _reset_ingest_state):
        yield

    @pytest.mark.anyio
    async def test_skipped_while_manual_ingest_running(self):
        """The daily refresh and manual ingest share one slot."""
        import api.main as main_mod
        from api.main import _daily_gtfs_refresh

        main_mod._ingest_state["running"] = True
        with patch("api.main.refresh_static_data", new_callable=AsyncMock) as mock_refresh:
            await _daily_gtfs_refresh()

        mock_refresh.assert_not_called()

    @pytest.mark.anyio
    async def test_calls_refresh_build_seed(self):
        """Job invokes refresh_static_data, build_graph, and seed_from_static."""
        from api.main import _daily_gtfs_refresh

        mock_session = MagicMock()
        with (
            patch("api.main.SessionLocal", return_value=mock_session),
            patch("api.main.refresh_static_data", new_callable=AsyncMock) as mock_refresh,
            patch("api.main.build_graph") as mock_build,
            patch("api.main.decay_reliability_records", return_value=3) as mock_decay,
            patch("api.main.seed_from_static", return_value=5) as mock_seed,
        ):
            await _daily_gtfs_refresh()

        mock_refresh.assert_called_once_with(mock_session)
        mock_build.assert_called_once_with(mock_session)
        mock_decay.assert_called_once_with(mock_session)
        mock_seed.assert_called_once_with(mock_session, fill_gaps_only=True)

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


# ---------------------------------------------------------------------------
# Route cache helpers
# ---------------------------------------------------------------------------

class TestRoutesCache:

    def setup_method(self):
        """Clear the module-level cache before each test."""
        from api.main import _clear_routes_cache
        _clear_routes_cache()

    def test_cache_key_includes_all_fields(self):
        from api.main import _routes_cache_key
        dt = datetime(2026, 2, 17, 8, 30, 0)
        key = _routes_cache_key("UN", "GL", dt)
        assert key == ("UN", "GL", "2026-02-17", "08:30")

    def test_cache_miss_returns_none(self):
        from api.main import _get_cached_routes
        assert _get_cached_routes(("UN", "GL", "2026-02-17", "08:30")) is None

    def test_store_and_retrieve(self):
        from api.main import _get_cached_routes, _store_cached_routes
        key = ("UN", "GL", "2026-02-17", "08:30")
        routes = [[{"kind": "trip", "route_id": "R1"}]]
        _store_cached_routes(key, routes)
        assert _get_cached_routes(key) == routes

    def test_clear_removes_entries(self):
        from api.main import _clear_routes_cache, _get_cached_routes, _store_cached_routes
        key = ("UN", "GL", "2026-02-17", "08:30")
        _store_cached_routes(key, [[]])
        _clear_routes_cache()
        assert _get_cached_routes(key) is None

    def test_expired_entry_returns_none(self, monkeypatch):
        from datetime import timedelta

        import api.main as main_mod
        from api.main import _get_cached_routes, _store_cached_routes

        key = ("UN", "GL", "2026-02-17", "08:30")
        # TTL is captured per entry at store time — shrink it before storing.
        monkeypatch.setattr(main_mod, "_ROUTES_CACHE_TTL", timedelta(seconds=0))
        _store_cached_routes(key, [[]])
        assert _get_cached_routes(key) is None

    def test_empty_result_negative_cached(self, client, monkeypatch):
        """Repeated queries for an unroutable pair must not re-run routing."""
        import api.main as main_mod

        calls = {"n": 0}

        def fake_find_routes(*args, **kwargs):
            calls["n"] += 1
            return []

        monkeypatch.setattr(main_mod, "find_routes", fake_find_routes)
        params = "origin=UN&destination=GL&travel_date=2026-02-18&departure_time=08:00"
        assert client.get(f"/routes?{params}").status_code == 404
        assert client.get(f"/routes?{params}").status_code == 404
        assert calls["n"] == 1  # second 404 came from the negative cache

    def test_negative_entries_use_short_ttl(self):
        import api.main as main_mod
        from api.main import _store_cached_routes

        key = ("UN", "GL", "2026-02-17", "08:30")
        _store_cached_routes(key, [])
        assert main_mod._routes_cache[key][2] == main_mod._ROUTES_CACHE_NEGATIVE_TTL

    def test_cache_size_is_bounded(self, monkeypatch):
        import api.main as main_mod
        from api.main import _store_cached_routes

        monkeypatch.setattr(main_mod, "_ROUTES_CACHE_MAX_ENTRIES", 20)
        for i in range(60):
            _store_cached_routes(("UN", f"S{i}", "2026-02-17", "08:30"), [["x"]])
        assert len(main_mod._routes_cache) <= 20

    def test_find_routes_called_once_on_cache_hit(self, client, monkeypatch):
        """Second identical request uses cached routes; find_routes called once."""
        import api.main as main_mod

        fake_legs = [{
            "kind": "trip",
            "from_stop_id": "UN", "to_stop_id": "GL",
            "from_stop_name": "Union", "to_stop_name": "Guelph",
            "trip_id": "T1", "route_id": "R1", "service_id": "20260217",
            "departure_time": "08:00:00", "arrival_time": "09:30:00",
            "travel_seconds": 5400,
        }]
        call_count = {"n": 0}

        def fake_find_routes(*args, **kwargs):
            call_count["n"] += 1
            return [fake_legs]

        monkeypatch.setattr(main_mod, "find_routes", fake_find_routes)
        monkeypatch.setattr(main_mod, "get_historical_reliability_batch", lambda *a, **kw: {})
        monkeypatch.setattr(main_mod, "compute_live_risk", lambda **kw: {
            "risk_score": 0.1, "risk_label": "Low", "modifiers": [], "is_cancelled": False,
        })

        params = "origin=UN&destination=GL&travel_date=2026-02-17&departure_time=08:00"
        client.get(f"/routes?{params}")
        client.get(f"/routes?{params}")

        assert call_count["n"] == 1

    def test_different_params_not_shared(self, monkeypatch):
        """Different origin/destination get independent cache entries."""
        from api.main import _get_cached_routes, _routes_cache_key

        key_a = _routes_cache_key("UN", "GL", datetime(2026, 2, 17, 8, 0))
        key_b = _routes_cache_key("BR", "GL", datetime(2026, 2, 17, 8, 0))
        from api.main import _store_cached_routes
        _store_cached_routes(key_a, [["route_a"]])
        assert _get_cached_routes(key_b) is None


class TestRouteCacheSingleFlight:
    def test_concurrent_identical_requests_compute_once(self, monkeypatch):
        """N concurrent cache misses on the same key run find_routes once;
        the others wait and reuse the cached result (single-flight)."""
        import threading
        import time
        from unittest.mock import MagicMock

        import api.main as main_mod

        main_mod._clear_routes_cache()
        calls = []
        walk_route = [[{
            "kind": "walk", "from_stop_name": "A", "to_stop_name": "B",
            "walk_seconds": 60, "distance_m": 80.0,
        }]]

        def slow_find(*args, **kwargs):
            calls.append(1)
            time.sleep(0.1)
            return walk_route

        monkeypatch.setattr(main_mod, "find_routes", slow_find)

        results = []
        def worker():
            results.append(main_mod._score_routes_blocking(
                "A", "B", datetime(2026, 2, 9, 8, 0), MagicMock()
            ))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(calls) == 1
        assert len(results) == 4
        assert all(r == results[0] for r in results)
        main_mod._clear_routes_cache()
