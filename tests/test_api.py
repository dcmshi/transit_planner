"""
Integration tests for API endpoints.

The FastAPI lifespan (init_db, build_graph, scheduler) is patched out
for every test.  Each test gets its own in-memory SQLite database via
the db_session / client fixtures, so tests are fully isolated.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.cache as cache_mod
import api.lifespan as lifespan_mod
import api.ratelimit as ratelimit_mod
import api.routes as routes_mod
from config import AGENCY_TZ
from db.models import Base, Stop
from db.session import get_session
from routing.engine import ArriveByResult, encode_polyline

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
        patch("api.lifespan.init_db"),
        patch("api.lifespan.build_graph"),
        patch("api.lifespan.SessionLocal", return_value=MagicMock()),
        # Belt and braces on top of conftest's GTFS_RT_API_KEY="" pin: the
        # lifespan must never fire real RT polls from unit tests.
        patch("api.lifespan.GTFS_RT_API_KEY", ""),
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
    from api.cache import _clear_routes_cache
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
        with patch("api.routes.get_graph", side_effect=RuntimeError("not built")):
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

    def test_like_wildcards_matched_literally(self, client, db_session):
        """A stray % or _ in the query must not change match semantics."""
        db_session.add(Stop(stop_id="P1", stop_name="100% Ave",
                            stop_lat=43.0, stop_lon=-79.0))
        db_session.add(Stop(stop_id="P2", stop_name="100 Percent Rd",
                            stop_lat=43.1, stop_lon=-79.1))
        db_session.commit()

        names = [s["stop_name"] for s in client.get("/stops?query=100%25").json()]
        assert names == ["100% Ave"]  # literal %, not "starts with 100"

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
# Dominance pruning
# ---------------------------------------------------------------------------

def _scored_route(dep, arr, transfers=0, risk=0.2, walk=0.0):
    return {
        "legs": [{"kind": "trip", "departure_time": dep, "arrival_time": arr}],
        "transfers": transfers,
        "risk_score": risk,
        "total_walk_metres": walk,
    }


class TestDominancePruning:
    def test_strictly_dominated_route_dropped(self):
        """Regression (live example): options departing together where one
        arrives 3h later with 2 extra transfers helped no rider."""
        from api.routes import _prune_dominated

        best = _scored_route("16:08:00", "17:35:00", transfers=0)
        worse = _scored_route("16:08:00", "20:35:00", transfers=2)
        assert _prune_dominated([worse, best]) == [best]

    def test_tradeoff_routes_both_kept(self):
        from api.routes import _prune_dominated

        early_risky = _scored_route("16:00:00", "17:00:00", risk=0.6)
        later_safe = _scored_route("16:30:00", "17:30:00", risk=0.2)
        assert len(_prune_dominated([early_risky, later_safe])) == 2

    def test_walk_heavy_route_does_not_dominate_zero_walk(self):
        """Regression (ninth pass): a 450m-walk option that departs later
        and arrives earlier must not delete the no-walk alternative — the
        rider may strongly prefer not walking."""
        from api.routes import _prune_dominated

        no_walk = _scored_route("10:00:00", "11:00:00", walk=0.0)
        walk_heavy = _scored_route("10:05:00", "10:55:00", walk=450.0)
        assert len(_prune_dominated([no_walk, walk_heavy])) == 2

    def test_identical_routes_both_kept(self):
        from api.routes import _prune_dominated

        a = _scored_route("16:00:00", "17:00:00")
        b = _scored_route("16:00:00", "17:00:00")
        assert len(_prune_dominated([a, b])) == 2  # ties don't dominate

    def test_survivors_sorted_by_arrival(self):
        from api.routes import _prune_dominated

        late = _scored_route("18:00:00", "19:00:00")
        early = _scored_route("16:00:00", "17:00:00")
        result = _prune_dominated([late, early])
        assert result == [early, late]

    def test_endpoint_drops_dominated_route(self, client):
        dominated_route = [{**_FAKE_ROUTE[0], "arrival_time": "11:30:00"}]
        with (
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE, dominated_route]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            body = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            ).json()

        # Same departure/transfers/risk, arrives 2h later → pruned.
        assert len(body["routes"]) == 1
        assert body["routes"][0]["legs"][0]["arrival_time"] == "09:21:00"


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
        ratelimit_mod._rate_buckets.clear()
        yield
        ratelimit_mod._rate_buckets.clear()

    def test_requests_over_limit_get_429(self, client):
        with patch("api.ratelimit.RATE_LIMIT_PER_MINUTE", 3):
            statuses = [
                client.get("/stops?query=Guelph").status_code for _ in range(4)
            ]
        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429

    def test_limit_disabled_when_zero(self, client):
        with patch("api.ratelimit.RATE_LIMIT_PER_MINUTE", 0):
            statuses = [
                client.get("/stops?query=Guelph").status_code for _ in range(5)
            ]
        assert statuses == [200] * 5

    def test_health_not_rate_limited(self, client):
        with patch("api.ratelimit.RATE_LIMIT_PER_MINUTE", 1):
            client.get("/stops?query=Guelph")  # consume the budget
            assert client.get("/health").status_code == 200

    def test_429_includes_retry_after(self, client):
        with patch("api.ratelimit.RATE_LIMIT_PER_MINUTE", 1):
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

        stale_ts = time_mod.monotonic() - 3600  # far outside the window
        for i in range(5):
            ratelimit_mod._rate_buckets[f"10.0.0.{i}"] = deque([stale_ts])

        with (
            patch("api.ratelimit.RATE_LIMIT_PER_MINUTE", 100),
            patch("api.ratelimit._RATE_BUCKETS_MAX", 3),  # force the cleanup pass
        ):
            client.get("/stops?query=Guelph")

        assert not any(k.startswith("10.0.0.") for k in ratelimit_mod._rate_buckets)


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
    "time_bucket": "weekday_am_peak",
    "scheduled_departures": 0.0, "observed_departures": 0.0,
    "total_delay_seconds": 0.0, "cancellation_count": 0.0,
    "source": None, "neutral_prior_used": True,
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

    # --- arrive_by ---

    def test_arrive_by_with_departure_time_returns_422(self, client):
        resp = client.get(
            "/routes?origin=UN&destination=GL&travel_date=2026-02-11"
            "&departure_time=08:00&arrive_by=10:00"
        )
        assert resp.status_code == 422
        assert "not both" in resp.json()["detail"].lower()

    @pytest.mark.parametrize("bad", ["notATime", "10", "99:00", "10:75", "10:00:99"])
    def test_invalid_arrive_by_returns_422(self, client, bad):
        resp = client.get(
            f"/routes?origin=UN&destination=GL&travel_date=2026-02-11&arrive_by={bad}"
        )
        assert resp.status_code == 422

    def test_arrive_by_routes_through_the_arrival_search(self, client):
        cache_mod._routes_cache.clear()
        with (
            patch("api.routes.find_routes_arriving_by",
                  return_value=ArriveByResult([_FAKE_ROUTE], True)) as mock_arrive,
            patch("api.routes.find_routes") as mock_depart,
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL&travel_date=2026-02-11&arrive_by=10:30"
            )
        assert resp.status_code == 200
        mock_depart.assert_not_called()
        assert mock_arrive.call_args.kwargs["arrive_by_sec"] == 10 * 3600 + 30 * 60
        assert mock_arrive.call_args.kwargs["travel_day"] == date(2026, 2, 11)

    def test_arrive_by_accepts_post_midnight_gtfs_hours(self, client):
        cache_mod._routes_cache.clear()
        with (
            patch("api.routes.find_routes_arriving_by",
                  return_value=ArriveByResult([_FAKE_ROUTE], True)) as mock_arrive,
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL&travel_date=2026-02-11&arrive_by=25:30"
            )
        assert resp.status_code == 200
        assert mock_arrive.call_args.kwargs["arrive_by_sec"] == 25 * 3600 + 30 * 60

    def test_arrive_by_and_depart_do_not_share_a_cache_entry(self, client):
        """"Depart at 09:00" and "arrive by 09:00" are different questions;
        the cache key must not conflate them."""
        cache_mod._routes_cache.clear()
        with (
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]) as mock_depart,
            patch("api.routes.find_routes_arriving_by",
                  return_value=ArriveByResult([_FAKE_ROUTE], True)) as mock_arrive,
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            base = "/routes?origin=UN&destination=GL&travel_date=2026-02-11"
            assert client.get(f"{base}&departure_time=09:00").status_code == 200
            assert client.get(f"{base}&arrive_by=09:00").status_code == 200

        assert mock_depart.call_count == 1
        assert mock_arrive.call_count == 1

    def test_each_deadline_gets_its_own_cache_entry(self, client):
        """Regression: departure_dt carries only the travel date under
        arrive_by, so every deadline for one origin/destination/date collapsed
        onto a single cache key.  The first answer was then served for every
        other deadline — including ones it arrives after — and a legitimately
        empty result negative-cached 404s over deadlines that do have service."""
        cache_mod._routes_cache.clear()
        seen = []

        def by_deadline(*args, **kwargs):
            seen.append(kwargs["arrive_by_sec"])
            return ArriveByResult([_FAKE_ROUTE], True)

        with (
            patch("api.routes.find_routes_arriving_by", side_effect=by_deadline),
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            base = "/routes?origin=GL&destination=UN&travel_date=2026-08-03"
            for deadline in ("10:35", "21:45", "23:59"):
                assert client.get(f"{base}&arrive_by={deadline}").status_code == 200

        assert seen == [10 * 3600 + 35 * 60, 21 * 3600 + 45 * 60, 23 * 3600 + 59 * 60]

    def test_empty_deadline_result_does_not_poison_other_deadlines(self, client):
        cache_mod._routes_cache.clear()
        calls = {"n": 0}

        def first_empty(*args, **kwargs):
            calls["n"] += 1
            if kwargs["arrive_by_sec"] < 12 * 3600:
                return ArriveByResult([], True)
            return ArriveByResult([_FAKE_ROUTE], True)

        with (
            patch("api.routes.find_routes_arriving_by", side_effect=first_empty),
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            base = "/routes?origin=GL&destination=UN&travel_date=2026-08-03"
            assert client.get(f"{base}&arrive_by=09:00").status_code == 404
            assert client.get(f"{base}&arrive_by=23:59").status_code == 200
        assert calls["n"] == 2  # the 404 did not answer the second query

    def test_missed_deadline_and_no_service_read_differently(self, client):
        """The client needs to tell "try a later deadline" from "these stops
        are not connected"."""
        cache_mod._routes_cache.clear()
        base = "/routes?origin=GL&destination=UN&travel_date=2026-08-03"

        with (
            patch("api.routes.find_routes_arriving_by",
                  return_value=ArriveByResult([], True)),
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            missed = client.get(f"{base}&arrive_by=09:00")
        cache_mod._routes_cache.clear()
        with (
            patch("api.routes.find_routes_arriving_by",
                  return_value=ArriveByResult([], False)),
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            unreachable = client.get(f"{base}&arrive_by=09:00")

        assert missed.status_code == unreachable.status_code == 404
        assert "09:00" in missed.json()["detail"]
        assert "later deadline" in missed.json()["detail"]
        assert missed.json()["detail"] != unreachable.json()["detail"]

    # --- routing errors ---

    def test_unknown_stop_returns_404(self, client):
        with patch("api.routes.find_routes",
                   side_effect=ValueError("Origin stop 'ZZ' not found in graph.")):
            resp = client.get(
                "/routes?origin=ZZ&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 404
        assert "ZZ" in resp.json()["detail"]

    def test_no_routes_found_returns_404(self, client):
        with patch("api.routes.find_routes", return_value=[]):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 404

    # --- valid response ---

    def test_valid_route_returns_200(self, client):
        with (
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 200

    def test_response_contains_routes_key(self, client):
        with (
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            body = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            ).json()

        assert "routes" in body
        assert len(body["routes"]) == 1

    def test_route_has_expected_fields(self, client):
        with (
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
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
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
        ):
            route = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            ).json()["routes"][0]

        assert route["total_travel_seconds"] == 4860

    def test_risk_score_and_label_present(self, client):
        with (
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
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
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}) as mock_hist,
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK) as mock_live,
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
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
            patch("api.routes.get_live_delay", return_value=300),
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
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
            patch("api.routes.get_live_delay", return_value=300),
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
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
            patch("api.routes.compute_live_risk", return_value=_FAKE_LIVE_RISK),
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
        cache_mod._routes_cache.clear()
        with patch("api.routes.find_routes", side_effect=RuntimeError("graph exploded")):
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

    def _reset():
        lifespan_mod._ingest_state.update(
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
            patch("api.routes.INGEST_API_KEY", ""),
            patch("api.lifespan._run_gtfs_ingest", new_callable=AsyncMock),
        ):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 202

    def test_correct_key_accepted(self, client):
        """Correct X-API-Key header → 202."""
        with (
            patch("api.routes.INGEST_API_KEY", "secret"),
            patch("api.lifespan._run_gtfs_ingest", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/ingest/gtfs-static",
                headers={"X-API-Key": "secret"},
            )
        assert resp.status_code == 202

    def test_wrong_key_rejected(self, client):
        """Wrong X-API-Key header → 401."""
        with patch("api.routes.INGEST_API_KEY", "secret"):
            resp = client.post(
                "/ingest/gtfs-static",
                headers={"X-API-Key": "wrong"},
            )
        assert resp.status_code == 401

    def test_missing_header_rejected(self, client):
        """No X-API-Key header when key is configured → 401."""
        with patch("api.routes.INGEST_API_KEY", "secret"):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 401


class TestIngestBackground:
    """202 semantics, the single-slot guard, and the status endpoint."""

    @pytest.fixture(autouse=True)
    def _state(self, _reset_ingest_state):
        yield

    def test_returns_202_accepted(self, client):
        with (
            patch("api.routes.INGEST_API_KEY", ""),
            patch("api.lifespan._run_gtfs_ingest", new_callable=AsyncMock),
        ):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_concurrent_ingest_rejected_409(self, client):
        lifespan_mod._ingest_state["running"] = True
        with patch("api.routes.INGEST_API_KEY", ""):
            resp = client.post("/ingest/gtfs-static")
        assert resp.status_code == 409

    def test_status_endpoint_reports_state(self, client):
        lifespan_mod._ingest_state.update(
            running=False, started_at="2026-07-10T12:00:00+00:00",
            finished_at="2026-07-10T12:01:00+00:00",
            last_status="ok", last_message="done",
        )
        with patch("api.routes.INGEST_API_KEY", ""):
            body = client.get("/ingest/status").json()
        assert body["running"] is False
        assert body["last_status"] == "ok"
        assert body["last_message"] == "done"

    @pytest.mark.anyio
    async def test_run_ingest_chains_refresh_build_seed(self):
        """The background body chains refresh → build → full reseed and
        records success in the ingest state."""
        from api.lifespan import _run_gtfs_ingest

        mock_session = MagicMock()
        lifespan_mod._ingest_state["running"] = True  # slot claimed by endpoint
        with (
            patch("api.lifespan.SessionLocal", return_value=mock_session),
            patch("api.lifespan.refresh_static_data", new_callable=AsyncMock) as mock_refresh,
            patch("api.lifespan.build_graph") as mock_build,
            patch("api.lifespan.seed_from_static", return_value=42) as mock_seed,
        ):
            await _run_gtfs_ingest()

        mock_refresh.assert_called_once_with(mock_session)
        mock_build.assert_called_once_with(mock_session)
        _, kwargs = mock_seed.call_args
        assert kwargs.get("fill_gaps_only") is False
        assert lifespan_mod._ingest_state["running"] is False
        assert lifespan_mod._ingest_state["last_status"] == "ok"
        assert "42" in lifespan_mod._ingest_state["last_message"]
        mock_session.close.assert_called_once()

    @pytest.mark.anyio
    async def test_cancelled_ingest_releases_slot(self):
        """Regression: CancelledError bypasses `except Exception`; a
        cancelled ingest task must not leave running=True forever (which
        would 409 every manual ingest and skip every daily refresh)."""
        import asyncio

        from api.lifespan import _run_gtfs_ingest

        lifespan_mod._ingest_state["running"] = True

        async def hang(session):
            await asyncio.Event().wait()

        with (
            patch("api.lifespan.SessionLocal", return_value=MagicMock()),
            patch("api.lifespan.refresh_static_data", side_effect=hang),
        ):
            task = asyncio.get_running_loop().create_task(_run_gtfs_ingest())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert lifespan_mod._ingest_state["running"] is False
        assert lifespan_mod._ingest_state["last_status"] == "error"
        assert "cancelled" in lifespan_mod._ingest_state["last_message"].lower()

    @pytest.mark.anyio
    async def test_run_ingest_records_error(self):
        from api.lifespan import _run_gtfs_ingest

        lifespan_mod._ingest_state["running"] = True
        with (
            patch("api.lifespan.SessionLocal", return_value=MagicMock()),
            patch("api.lifespan.refresh_static_data", new_callable=AsyncMock,
                  side_effect=Exception("feed down")),
        ):
            await _run_gtfs_ingest()  # must not raise

        assert lifespan_mod._ingest_state["running"] is False
        assert lifespan_mod._ingest_state["last_status"] == "error"
        assert "feed down" in lifespan_mod._ingest_state["last_message"]

# ---------------------------------------------------------------------------
# _daily_gtfs_refresh job function
# ---------------------------------------------------------------------------

class TestDailyRefreshTrigger:
    """The refresh trigger must be anchored to wall-clock time.

    With an interval trigger APScheduler set the first fire to
    boot + GTFS_REFRESH_HOURS, so any restart inside that window pushed it
    out again and a daily-restarting process never refreshed — nor decayed
    reliability counters, nor cleared the route cache, since this job is the
    only caller of both.
    """

    def _next(self, trigger, now):
        return trigger.get_next_fire_time(None, now)

    def test_daily_fires_at_the_fixed_agency_hour(self):
        from api.lifespan import _DAILY_REFRESH_HOUR, _daily_refresh_trigger

        trigger = _daily_refresh_trigger()
        now = datetime(2026, 7, 31, 9, 17, tzinfo=AGENCY_TZ)
        nxt = self._next(trigger, now)
        assert nxt.hour == _DAILY_REFRESH_HOUR
        assert nxt.minute == 0

    def test_fire_time_is_independent_of_boot_time(self):
        """Two processes booting hours apart must target the same instant —
        the property the interval trigger lacked."""
        from api.lifespan import _daily_refresh_trigger

        trigger = _daily_refresh_trigger()
        early = self._next(trigger, datetime(2026, 7, 31, 4, 0, tzinfo=AGENCY_TZ))
        late = self._next(trigger, datetime(2026, 7, 31, 23, 30, tzinfo=AGENCY_TZ))
        assert early == late

    def test_restart_does_not_push_the_run_a_full_window_away(self):
        from api.lifespan import _daily_refresh_trigger

        trigger = _daily_refresh_trigger()
        now = datetime(2026, 7, 31, 2, 55, tzinfo=AGENCY_TZ)
        nxt = self._next(trigger, now)
        assert (nxt - now).total_seconds() == 5 * 60  # 5 minutes, not 24 hours

    def test_sub_daily_refresh_hours_use_a_step_schedule(self):
        from api.lifespan import _daily_refresh_trigger

        with patch("api.lifespan.GTFS_REFRESH_HOURS", 6):
            trigger = _daily_refresh_trigger()
        now = datetime(2026, 7, 31, 7, 30, tzinfo=AGENCY_TZ)
        nxt = self._next(trigger, now)
        assert (nxt.hour, nxt.minute) == (12, 0)

    def test_zero_refresh_hours_does_not_crash_the_trigger(self):
        from api.lifespan import _daily_refresh_trigger

        with patch("api.lifespan.GTFS_REFRESH_HOURS", 0):
            trigger = _daily_refresh_trigger()
        assert self._next(trigger, datetime(2026, 7, 31, 7, 30, tzinfo=AGENCY_TZ)) is not None


class TestDailyGtfsRefreshJob:

    @pytest.fixture(autouse=True)
    def _state(self, _reset_ingest_state):
        yield

    @pytest.mark.anyio
    async def test_skipped_while_manual_ingest_running(self):
        """The daily refresh and manual ingest share one slot."""
        from api.lifespan import _daily_gtfs_refresh

        lifespan_mod._ingest_state["running"] = True
        with patch("api.lifespan.refresh_static_data", new_callable=AsyncMock) as mock_refresh:
            await _daily_gtfs_refresh()

        mock_refresh.assert_not_called()

    @pytest.mark.anyio
    async def test_calls_refresh_build_seed(self):
        """Job invokes refresh_static_data, build_graph, and seed_from_static."""
        from api.lifespan import _daily_gtfs_refresh

        mock_session = MagicMock()
        with (
            patch("api.lifespan.SessionLocal", return_value=mock_session),
            patch("api.lifespan.refresh_static_data", new_callable=AsyncMock) as mock_refresh,
            patch("api.lifespan.build_graph") as mock_build,
            patch("api.lifespan.decay_reliability_records", return_value=3) as mock_decay,
            patch("api.lifespan.seed_from_static", return_value=5) as mock_seed,
        ):
            await _daily_gtfs_refresh()

        mock_refresh.assert_called_once_with(mock_session)
        mock_build.assert_called_once_with(mock_session)
        mock_decay.assert_called_once_with(mock_session)
        mock_seed.assert_called_once_with(mock_session, fill_gaps_only=True)

    @pytest.mark.anyio
    async def test_error_does_not_propagate(self):
        """A failure during refresh is swallowed — the job must not crash the scheduler."""
        from api.lifespan import _daily_gtfs_refresh

        with (
            patch("api.lifespan.SessionLocal", return_value=MagicMock()),
            patch("api.lifespan.refresh_static_data", new_callable=AsyncMock,
                  side_effect=Exception("network down")),
            patch("api.lifespan.build_graph"),
            patch("api.lifespan.seed_from_static"),
        ):
            await _daily_gtfs_refresh()  # must not raise

    @pytest.mark.anyio
    async def test_session_always_closed(self):
        """DB session is closed in the finally block even when the job fails."""
        from api.lifespan import _daily_gtfs_refresh

        mock_session = MagicMock()
        with (
            patch("api.lifespan.SessionLocal", return_value=mock_session),
            patch("api.lifespan.refresh_static_data", new_callable=AsyncMock,
                  side_effect=Exception("fail")),
            patch("api.lifespan.build_graph"),
            patch("api.lifespan.seed_from_static"),
        ):
            await _daily_gtfs_refresh()

        mock_session.close.assert_called_once()


# ---------------------------------------------------------------------------
# Route cache helpers
# ---------------------------------------------------------------------------

class TestRoutesCache:

    def setup_method(self):
        """Clear the module-level cache before each test."""
        from api.cache import _clear_routes_cache
        _clear_routes_cache()

    def test_cache_key_includes_all_fields(self):
        from api.cache import _routes_cache_key
        dt = datetime(2026, 2, 17, 8, 30, 0)
        key = _routes_cache_key("UN", "GL", dt)
        assert key == ("UN", "GL", "2026-02-17", "08:30", "depart")

    def test_cache_miss_returns_none(self):
        from api.cache import _get_cached_routes
        assert _get_cached_routes(("UN", "GL", "2026-02-17", "08:30", "depart")) is None

    def test_store_and_retrieve(self):
        from api.cache import _get_cached_routes, _store_cached_routes
        key = ("UN", "GL", "2026-02-17", "08:30", "depart")
        routes = [[{"kind": "trip", "route_id": "R1"}]]
        _store_cached_routes(key, routes)
        assert _get_cached_routes(key) == routes

    def test_clear_removes_entries(self):
        from api.cache import _clear_routes_cache, _get_cached_routes, _store_cached_routes
        key = ("UN", "GL", "2026-02-17", "08:30", "depart")
        _store_cached_routes(key, [[]])
        _clear_routes_cache()
        assert _get_cached_routes(key) is None

    def test_expired_entry_returns_none(self, monkeypatch):
        from datetime import timedelta

        from api.cache import _get_cached_routes, _store_cached_routes

        key = ("UN", "GL", "2026-02-17", "08:30", "depart")
        # TTL is captured per entry at store time — shrink it before storing.
        monkeypatch.setattr(cache_mod, "_ROUTES_CACHE_TTL", timedelta(seconds=0))
        _store_cached_routes(key, [[]])
        assert _get_cached_routes(key) is None

    def test_empty_result_negative_cached(self, client, monkeypatch):
        """Repeated queries for an unroutable pair must not re-run routing."""

        calls = {"n": 0}

        def fake_find_routes(*args, **kwargs):
            calls["n"] += 1
            return []

        monkeypatch.setattr(routes_mod, "find_routes", fake_find_routes)
        params = "origin=UN&destination=GL&travel_date=2026-02-18&departure_time=08:00"
        assert client.get(f"/routes?{params}").status_code == 404
        assert client.get(f"/routes?{params}").status_code == 404
        assert calls["n"] == 1  # second 404 came from the negative cache

    def test_negative_entries_use_short_ttl(self):
        from api.cache import _store_cached_routes

        key = ("UN", "GL", "2026-02-17", "08:30", "depart")
        _store_cached_routes(key, [])
        assert cache_mod._routes_cache[key][2] == cache_mod._ROUTES_CACHE_NEGATIVE_TTL

    def test_cache_size_is_bounded(self, monkeypatch):
        from api.cache import _store_cached_routes

        monkeypatch.setattr(cache_mod, "_ROUTES_CACHE_MAX_ENTRIES", 20)
        for i in range(60):
            _store_cached_routes(("UN", f"S{i}", "2026-02-17", "08:30", "depart"), [["x"]])
        assert len(cache_mod._routes_cache) <= 20

    def test_find_routes_called_once_on_cache_hit(self, client, monkeypatch):
        """Second identical request uses cached routes; find_routes called once."""

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

        monkeypatch.setattr(routes_mod, "find_routes", fake_find_routes)
        monkeypatch.setattr(routes_mod, "get_reliability_snapshots", lambda *a, **kw: {})
        monkeypatch.setattr(routes_mod, "compute_live_risk", lambda **kw: {
            "risk_score": 0.1, "risk_label": "Low", "modifiers": [], "is_cancelled": False,
            "time_bucket": "weekday_am_peak",
            "scheduled_departures": 0.0, "observed_departures": 0.0,
            "total_delay_seconds": 0.0, "cancellation_count": 0.0,
            "source": None, "neutral_prior_used": True,
        })

        params = "origin=UN&destination=GL&travel_date=2026-02-17&departure_time=08:00"
        client.get(f"/routes?{params}")
        client.get(f"/routes?{params}")

        assert call_count["n"] == 1

    def test_different_params_not_shared(self, monkeypatch):
        """Different origin/destination get independent cache entries."""
        from api.cache import _get_cached_routes, _routes_cache_key

        key_a = _routes_cache_key("UN", "GL", datetime(2026, 2, 17, 8, 0))
        key_b = _routes_cache_key("BR", "GL", datetime(2026, 2, 17, 8, 0))
        from api.cache import _store_cached_routes
        _store_cached_routes(key_a, [["route_a"]])
        assert _get_cached_routes(key_b) is None


class TestRouteCacheSingleFlight:
    def test_concurrent_identical_requests_compute_once(self, monkeypatch):
        """N concurrent cache misses on the same key run find_routes once;
        the others wait and reuse the cached result (single-flight)."""
        import threading
        import time
        from unittest.mock import MagicMock

        cache_mod._clear_routes_cache()
        calls = []
        walk_route = [[{
            "kind": "walk", "from_stop_name": "A", "to_stop_name": "B",
            "walk_seconds": 60, "distance_m": 80.0,
        }]]

        def slow_find(*args, **kwargs):
            calls.append(1)
            time.sleep(0.1)
            return walk_route

        monkeypatch.setattr(routes_mod, "find_routes", slow_find)

        results = []
        def worker():
            results.append(routes_mod._score_routes_blocking(
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
        cache_mod._clear_routes_cache()


# ---------------------------------------------------------------------------
# GET /reliability
# ---------------------------------------------------------------------------

class TestReliabilityEndpoint:
    """Read-only view of the counters behind a route's score. /health reports
    only aggregate counts by source, which cannot tell you whether a route
    scores badly from real observations or from a synthetic prior."""

    def _seed(self, db, **overrides):
        from db.models import ReliabilityRecord

        fields = dict(
            route_id="R1", stop_id="S1", time_bucket="weekday_am_peak",
            source="observed", scheduled_departures=100, observed_departures=95,
            total_delay_seconds=0, cancellation_count=0,
            window_start_date="20260201", window_end_date="20260214",
            updated_at="2026-02-14T00:00:00+00:00",
        )
        fields.update(overrides)
        db.add(ReliabilityRecord(**fields))
        db.commit()

    def test_requires_a_route_or_stop_filter(self, client):
        """Bounded lookup, not a table export."""
        resp = client.get("/reliability")
        assert resp.status_code == 422
        assert "route_id or stop_id" in resp.json()["detail"]

    def test_returns_counters_and_derived_score(self, client, db_session):
        self._seed(db_session)
        body = client.get("/reliability?route_id=R1").json()
        assert len(body) == 1
        rec = body[0]
        assert rec["route_id"] == "R1"
        assert rec["source"] == "observed"
        assert rec["scheduled_departures"] == 100
        assert rec["observed_departures"] == 95
        assert rec["score"] == pytest.approx(0.95)
        assert rec["neutral_prior_used"] is False

    def test_flags_records_too_sparse_to_score(self, client, db_session):
        """A record below the scoring threshold is where the neutral prior is
        substituted — the endpoint must distinguish that from a real 0.8."""
        self._seed(db_session, scheduled_departures=0, observed_departures=0)
        rec = client.get("/reliability?route_id=R1").json()[0]
        assert rec["score"] is None
        assert rec["neutral_prior_used"] is True

    def test_filters_by_stop_and_bucket(self, client, db_session):
        self._seed(db_session, stop_id="S1", time_bucket="weekday_am_peak")
        self._seed(db_session, stop_id="S2", time_bucket="weekend")

        assert len(client.get("/reliability?route_id=R1").json()) == 2
        assert len(client.get("/reliability?route_id=R1&stop_id=S2").json()) == 1
        by_bucket = client.get("/reliability?route_id=R1&time_bucket=weekend").json()
        assert [r["stop_id"] for r in by_bucket] == ["S2"]

    def test_unknown_route_returns_empty_list(self, client, db_session):
        self._seed(db_session)
        assert client.get("/reliability?route_id=NOPE").json() == []

    def test_limit_is_honoured_and_capped(self, client, db_session):
        for i in range(5):
            self._seed(db_session, stop_id=f"S{i}",
                       updated_at=f"2026-02-1{i}T00:00:00+00:00")
        assert len(client.get("/reliability?route_id=R1&limit=2").json()) == 2
        over = client.get(f"/reliability?route_id=R1&limit={routes_mod._RELIABILITY_MAX_LIMIT + 1}")
        assert over.status_code == 422
        assert client.get("/reliability?route_id=R1&limit=0").status_code == 422

    def test_newest_record_first(self, client, db_session):
        self._seed(db_session, stop_id="OLD", updated_at="2026-02-01T00:00:00+00:00")
        self._seed(db_session, stop_id="NEW", updated_at="2026-02-20T00:00:00+00:00")
        assert [r["stop_id"] for r in client.get("/reliability?route_id=R1").json()] == [
            "NEW", "OLD",
        ]

    def test_null_counters_do_not_error(self, client, db_session):
        """Counters are nullable; a row written by raw SQL can hold NULL."""
        from sqlalchemy import text

        db_session.execute(text(
            "INSERT INTO reliability_records (route_id, stop_id, time_bucket, source) "
            "VALUES ('R1', 'S1', 'weekday_am_peak', 'observed')"
        ))
        db_session.commit()
        rec = client.get("/reliability?route_id=R1").json()[0]
        assert rec["scheduled_departures"] == 0.0
        assert rec["score"] is None


# ---------------------------------------------------------------------------
# /stops routes_served, from the materialised stop_routes lookup
# ---------------------------------------------------------------------------

class TestStopRoutesLookup:
    """routes_served used to come from a DISTINCT over the stop_times/trips
    join — ~72,000 rows scanned per request to produce a few dozen pairs.
    It is now materialised at ingest and simply read back."""

    def _seed(self, db):
        from db.models import Route, StopRoute

        db.add(Stop(stop_id="UN", stop_name="Union Station GO",
                    stop_lat=43.6453, stop_lon=-79.3806))
        db.add(Stop(stop_id="GL", stop_name="Guelph Central GO",
                    stop_lat=43.5448, stop_lon=-80.2482))
        for rid in ("R1", "R2"):
            db.add(Route(route_id=rid, route_short_name=rid, route_long_name="", route_type=3))
        db.add(StopRoute(stop_id="UN", route_id="R2"))
        db.add(StopRoute(stop_id="UN", route_id="R1"))
        db.commit()

    def test_routes_served_comes_from_the_lookup(self, client, db_session):
        self._seed(db_session)
        result = client.get("/stops?query=Union").json()[0]
        assert result["routes_served"] == ["R1", "R2"]  # sorted for stable output

    def test_stop_with_no_routes_returns_empty_list(self, client, db_session):
        self._seed(db_session)
        result = client.get("/stops?query=Guelph").json()[0]
        assert result["routes_served"] == []

    def test_lookup_is_not_consulted_for_other_stops(self, client, db_session):
        self._seed(db_session)
        by_name = {s["stop_name"]: s["routes_served"]
                   for s in client.get("/stops?query=GO").json()}
        assert by_name["Union Station GO"] == ["R1", "R2"]
        assert by_name["Guelph Central GO"] == []


# ---------------------------------------------------------------------------
# Leg coordinates on /routes
# ---------------------------------------------------------------------------

class TestLegCoordinatesInResponse:
    """A map client needs [lon, lat] per leg endpoint. Without these it had to
    resolve every intermediate stop through /stops — five extra requests on a
    single planning flow, enough to hit the rate limit."""

    _LEG_WITH_COORDS = {
        **_FAKE_ROUTE[0],
        "from_lat": 43.6453, "from_lon": -79.3806,
        "to_lat": 43.5443, "to_lon": -80.2469,
    }

    def _get(self, client, legs):
        with (
            patch("api.routes.find_routes", return_value=[legs]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            return client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )

    def test_coordinates_are_serialised(self, client):
        resp = self._get(client, [self._LEG_WITH_COORDS])
        assert resp.status_code == 200
        leg = resp.json()["routes"][0]["legs"][0]
        assert (leg["from_lat"], leg["from_lon"]) == (43.6453, -79.3806)
        assert (leg["to_lat"], leg["to_lon"]) == (43.5443, -80.2469)

    def test_walk_legs_carry_them_too(self, client):
        walk = {
            "kind": "walk",
            "from_stop_id": "UN", "to_stop_id": "GL",
            "from_stop_name": "Union Station GO", "to_stop_name": "Guelph Central GO",
            "from_lat": 43.6453, "from_lon": -79.3806,
            "to_lat": 43.5443, "to_lon": -80.2469,
            "distance_m": 250.0, "walk_seconds": 200,
        }
        resp = self._get(client, [self._LEG_WITH_COORDS, walk])
        assert resp.status_code == 200
        rendered = resp.json()["routes"][0]["legs"][1]
        assert rendered["kind"] == "walk"
        assert (rendered["from_lat"], rendered["to_lon"]) == (43.6453, -80.2469)

    def test_absent_coordinates_serialise_as_null(self, client):
        """Legs built before the field existed, or a node without lat/lon,
        must not fail response validation."""
        resp = self._get(client, list(_FAKE_ROUTE))
        assert resp.status_code == 200
        leg = resp.json()["routes"][0]["legs"][0]
        assert leg["from_lat"] is None
        assert leg["to_lon"] is None

    def test_openapi_advertises_the_fields(self, client):
        """The frontend generates its types from /openapi.json."""
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        for model in ("TripLeg", "WalkLeg"):
            props = schemas[model]["properties"]
            for field in ("from_lat", "from_lon", "to_lat", "to_lon"):
                assert field in props, f"{model}.{field} missing from OpenAPI"
                assert {"type": "number"} in props[field]["anyOf"]


# ---------------------------------------------------------------------------
# Track geometry on /routes legs
# ---------------------------------------------------------------------------

class TestLegGeometryInResponse:
    """Each leg carries its own slice of the trip's GTFS shape, so the map can
    follow the track and still colour per leg by risk."""

    # Encoded polyline for [[-80.2469,43.5443],[-80.1,43.58],[-79.3806,43.6453]]
    _GEOM = encode_polyline([[-80.2469, 43.5443], [-80.1, 43.58], [-79.3806, 43.6453]])

    def _get(self, client, legs):
        with (
            patch("api.routes.find_routes", return_value=[legs]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            return client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )

    def test_geometry_is_serialised(self, client):
        leg = {**_FAKE_ROUTE[0], "geometry": self._GEOM}
        resp = self._get(client, [leg])
        assert resp.status_code == 200
        assert resp.json()["routes"][0]["legs"][0]["geometry"] == self._GEOM

    def test_leg_without_geometry_still_returns(self, client):
        """Partial coverage is explicitly fine — a trip with no shape must not
        fail the response, the client falls back to a straight chord."""
        resp = self._get(client, list(_FAKE_ROUTE))
        assert resp.status_code == 200
        assert resp.json()["routes"][0]["legs"][0]["geometry"] is None

    def test_mixed_coverage_in_one_route(self, client):
        with_geom = {**_FAKE_ROUTE[0], "geometry": self._GEOM}
        without = {**_FAKE_ROUTE[0], "trip_id": "T2"}
        resp = self._get(client, [with_geom, without])
        assert resp.status_code == 200
        legs = resp.json()["routes"][0]["legs"]
        assert legs[0]["geometry"] == self._GEOM
        assert legs[1]["geometry"] is None

    def test_walk_legs_have_no_geometry_field_set(self, client):
        """Non-goal by agreement: GTFS publishes no pedestrian geometry."""
        walk = {
            "kind": "walk",
            "from_stop_id": "UN", "to_stop_id": "GL",
            "from_stop_name": "Union Station GO", "to_stop_name": "Guelph Central GO",
            "from_lat": 43.6453, "from_lon": -79.3806,
            "to_lat": 43.5443, "to_lon": -80.2469,
            "distance_m": 250.0, "walk_seconds": 200,
        }
        resp = self._get(client, [{**_FAKE_ROUTE[0], "geometry": self._GEOM}, walk])
        assert resp.status_code == 200
        assert "geometry" not in resp.json()["routes"][0]["legs"][1]

    def test_openapi_advertises_geometry(self, client):
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        prop = schemas["TripLeg"]["properties"]["geometry"]
        # encoded polyline string, nullable
        assert {"type": "null"} in prop["anyOf"]
        assert {"type": "string"} in prop["anyOf"]
        assert "geometry" not in schemas["WalkLeg"]["properties"]


# ---------------------------------------------------------------------------
# LiveRisk.time_bucket <-> /reliability
# ---------------------------------------------------------------------------

class TestRiskTimeBucketContract:
    """The whole point of the field: fetch /reliability for the leg's
    (route_id, stop_id) and pick the row that produced the score."""

    def _seed_all_buckets(self, db):
        from db.models import ReliabilityRecord

        for bucket in ("weekday_am_peak", "weekday_pm_peak",
                       "weekday_offpeak", "weekend"):
            db.add(ReliabilityRecord(
                route_id="GT1", stop_id="UN", time_bucket=bucket, source="observed",
                scheduled_departures=100, observed_departures=95,
                total_delay_seconds=0, cancellation_count=0,
                updated_at="2026-02-14T00:00:00+00:00",
            ))
        db.commit()

    def test_leg_bucket_selects_exactly_one_reliability_row(self, client, db_session):
        self._seed_all_buckets(db_session)

        with (
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.status_code == 200
        leg = resp.json()["routes"][0]["legs"][0]
        bucket = leg["risk"]["time_bucket"]

        rows = client.get("/reliability?route_id=GT1&stop_id=UN").json()
        assert len(rows) == 4, "all four buckets exist for this pair"
        matching = [r for r in rows if r["time_bucket"] == bucket]
        assert len(matching) == 1, f"{bucket} should select exactly one row"

    def test_bucket_reflects_the_legs_departure_not_the_query_time(self, client, db_session):
        """_FAKE_ROUTE departs 08:00 — am peak — regardless of when asked."""
        self._seed_all_buckets(db_session)
        with (
            patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]),
            patch("api.routes.get_reliability_snapshots", return_value={}),
        ):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        assert resp.json()["routes"][0]["legs"][0]["risk"]["time_bucket"] == "weekday_am_peak"

    def test_leg_counters_match_the_reliability_row(self, client, db_session):
        """The counters inlined on the leg must be the same numbers
        /reliability reports for the row the bucket selects."""
        self._seed_all_buckets(db_session)

        with patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        risk = resp.json()["routes"][0]["legs"][0]["risk"]
        row = next(
            r for r in client.get("/reliability?route_id=GT1&stop_id=UN").json()
            if r["time_bucket"] == risk["time_bucket"]
        )
        for field in ("scheduled_departures", "observed_departures",
                      "total_delay_seconds", "cancellation_count",
                      "source", "neutral_prior_used"):
            assert risk[field] == row[field], field
        assert risk["risk_score"] == pytest.approx(1 - row["score"], abs=1e-6)

    def test_leg_with_no_history_reads_as_no_observations(self, client, db_session):
        """Nothing seeded: zeros and the neutral prior, so the UI can say "no
        observations yet" rather than showing an unexplained score."""
        with patch("api.routes.find_routes", return_value=[_FAKE_ROUTE]):
            resp = client.get(
                "/routes?origin=UN&destination=GL"
                "&travel_date=2026-02-11&departure_time=08:00"
            )
        risk = resp.json()["routes"][0]["legs"][0]["risk"]
        assert risk["scheduled_departures"] == 0
        assert risk["source"] is None
        assert risk["neutral_prior_used"] is True

    def test_openapi_advertises_the_counters(self, client):
        schema = client.get("/openapi.json").json()["components"]["schemas"]["LiveRisk"]
        for field in ("scheduled_departures", "observed_departures",
                      "total_delay_seconds", "cancellation_count",
                      "source", "neutral_prior_used"):
            assert field in schema["properties"], field
            assert field in schema["required"], field

    def test_openapi_advertises_time_bucket(self, client):
        props = client.get("/openapi.json").json()["components"]["schemas"]["LiveRisk"]["properties"]
        assert props["time_bucket"]["type"] == "string"
        required = client.get("/openapi.json").json()["components"]["schemas"]["LiveRisk"]["required"]
        assert "time_bucket" in required
