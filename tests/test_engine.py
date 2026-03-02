"""
Unit tests for routing.engine pure functions.

These tests have no DB or graph dependency — they exercise only the
logic that lives entirely inside routing/engine.py.
"""

import pytest

import networkx as nx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config import MAX_TRANSFERS, MIN_TRANSFER_MINUTES
from db.models import Base, Route, ServiceCalendarDate, Stop, StopTime, Trip
from routing.engine import (
    _RouteQueryCache,
    _fill_later_departures,
    _find_trip_legs,
    _hms_to_seconds,
    _passes_filters,
    _pick_longest_route,
    _route_signature,
    count_transfers,
    total_travel_seconds,
    total_walk_metres,
)


# ---------------------------------------------------------------------------
# Helpers to build minimal leg dicts
# ---------------------------------------------------------------------------

def _trip(route_id: str, dep: str, arr: str, travel_seconds: int, trip_id: str = "T1") -> dict:
    return {
        "kind": "trip",
        "from_stop_id": "A",
        "to_stop_id": "B",
        "from_stop_name": "Stop A",
        "to_stop_name": "Stop B",
        "trip_id": trip_id,
        "route_id": route_id,
        "service_id": "20260211",
        "departure_time": dep,
        "arrival_time": arr,
        "travel_seconds": travel_seconds,
    }


def _walk(walk_seconds: int = 300) -> dict:
    return {
        "kind": "walk",
        "from_stop_id": "A",
        "to_stop_id": "B",
        "from_stop_name": "Stop A",
        "to_stop_name": "Stop B",
        "distance_m": 250.0,
        "walk_seconds": walk_seconds,
    }


# ---------------------------------------------------------------------------
# _hms_to_seconds
# ---------------------------------------------------------------------------

class TestHmsToSeconds:
    def test_normal_time(self):
        assert _hms_to_seconds("08:30:00") == 8 * 3600 + 30 * 60

    def test_midnight(self):
        assert _hms_to_seconds("00:00:00") == 0

    def test_end_of_day(self):
        assert _hms_to_seconds("23:59:59") == 23 * 3600 + 59 * 60 + 59

    def test_over_24h(self):
        # GTFS allows times past midnight for overnight trips
        assert _hms_to_seconds("25:10:00") == 25 * 3600 + 10 * 60

    def test_with_seconds(self):
        assert _hms_to_seconds("09:05:30") == 9 * 3600 + 5 * 60 + 30

    def test_invalid_string_returns_zero(self):
        assert _hms_to_seconds("not-a-time") == 0

    def test_empty_string_returns_zero(self):
        assert _hms_to_seconds("") == 0

    def test_partial_string_returns_zero(self):
        assert _hms_to_seconds("08:30") == 0

    def test_none_returns_zero(self):
        # None triggers AttributeError on .strip() — now caught explicitly
        assert _hms_to_seconds(None) == 0


# ---------------------------------------------------------------------------
# total_travel_seconds
# ---------------------------------------------------------------------------

class TestTotalTravelSeconds:
    def test_empty_route(self):
        assert total_travel_seconds([]) == 0

    def test_single_trip_leg(self):
        legs = [_trip("R1", "08:00:00", "09:00:00", 3600)]
        assert total_travel_seconds(legs) == 3600

    def test_walk_only(self):
        # No trip legs — wall-clock can't be measured from transit times
        assert total_travel_seconds([_walk(300)]) == 0

    def test_trip_plus_walk(self):
        # Trailing walk excluded: wall-clock = last trip arrival − first trip departure
        legs = [_trip("R1", "08:00:00", "09:00:00", 3600), _walk(300)]
        assert total_travel_seconds(legs) == 3600

    def test_trip_walk_trip(self):
        # Transfer wait (09:00→09:15) IS included — that's time the commuter spends
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600),
            _walk(300),
            _trip("R2", "09:15:00", "10:00:00", 2700),
        ]
        assert total_travel_seconds(legs) == 7200  # 08:00→10:00 wall-clock

    def test_long_transfer_wait_counted(self):
        # Regression: a route with a 5-hour wait should show true door-to-door time,
        # not just active travel time — the bug that caused bad LLM recommendations
        legs = [
            _trip("R1", "09:07:00", "09:50:00", 2580),
            _walk(240),
            _trip("R1", "15:20:00", "15:46:00", 1560),
            _trip("R2", "16:51:00", "17:50:00", 3540),
        ]
        # 09:07 → 17:50 = 8h 43m = 31 380 s  (not 2580+240+1560+3540 = 7920 s)
        assert total_travel_seconds(legs) == 31_380


# ---------------------------------------------------------------------------
# _passes_filters
# ---------------------------------------------------------------------------

class TestPassesFilters:
    # --- must have at least one trip leg ---

    def test_empty_fails(self):
        assert _passes_filters([]) is False

    def test_walk_only_fails(self):
        assert _passes_filters([_walk()]) is False

    # --- zero-second legs are allowed (GTFS 1-minute rounding artifact) ---

    def test_zero_second_leg_passes(self):
        # Two stops sharing the same scheduled minute is valid GTFS data.
        legs = [_trip("R1", "08:00:00", "08:00:00", 0)]
        assert _passes_filters(legs) is True

    def test_nonzero_leg_passes(self):
        legs = [_trip("R1", "08:00:00", "09:00:00", 3600)]
        assert _passes_filters(legs) is True

    # --- transfer counting (route_id changes) ---

    def test_same_route_id_no_transfer(self):
        # Two consecutive legs on the same route_id = 0 transfers → passes
        legs = [
            _trip("R1", "08:00:00", "08:30:00", 1800),
            _trip("R1", "08:30:00", "09:00:00", 1800),
        ]
        assert _passes_filters(legs) is True

    def test_one_transfer_with_enough_buffer(self):
        # Transfer with more than MIN_TRANSFER_MINUTES buffer
        dep2_sec = 9 * 3600 + (MIN_TRANSFER_MINUTES + 5) * 60
        h = dep2_sec // 3600
        m = (dep2_sec % 3600) // 60
        s = dep2_sec % 60
        dep2 = f"{h:02d}:{m:02d}:{s:02d}"
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600),
            _trip("R2", dep2, "10:00:00", 3600),
        ]
        assert _passes_filters(legs) is True

    def test_tight_transfer_fails(self):
        # Transfer with only 5 min buffer — below MIN_TRANSFER_MINUTES (10)
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600),
            _trip("R2", "09:05:00", "10:00:00", 3600),
        ]
        assert _passes_filters(legs) is False

    def test_exact_min_buffer_passes(self):
        # Transfer with exactly MIN_TRANSFER_MINUTES buffer
        dep2_sec = 9 * 3600 + MIN_TRANSFER_MINUTES * 60
        h = dep2_sec // 3600
        m = (dep2_sec % 3600) // 60
        s = dep2_sec % 60
        dep2 = f"{h:02d}:{m:02d}:{s:02d}"
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600),
            _trip("R2", dep2, "10:00:00", 3600),
        ]
        assert _passes_filters(legs) is True

    def test_too_many_transfers_fails(self):
        # MAX_TRANSFERS + 1 route changes (4 different routes = 3 transfers > MAX_TRANSFERS=2)
        times = [
            ("08:00:00", "09:00:00", 3600),
            ("09:30:00", "10:30:00", 3600),
            ("11:00:00", "12:00:00", 3600),
            ("12:30:00", "13:30:00", 3600),
        ]
        legs = [_trip(f"R{i}", dep, arr, sec) for i, (dep, arr, sec) in enumerate(times)]
        assert _passes_filters(legs) is False

    def test_max_transfers_exactly_passes(self):
        # Exactly MAX_TRANSFERS route changes with sufficient buffer
        times = [
            ("08:00:00", "09:00:00", 3600),
            ("09:30:00", "10:30:00", 3600),
            ("11:00:00", "12:00:00", 3600),
        ]
        legs = [_trip(f"R{i}", dep, arr, sec) for i, (dep, arr, sec) in enumerate(times)]
        assert _passes_filters(legs) is True

    def test_walk_legs_ignored_in_transfer_count(self):
        # Walk between two same-route trip legs — walk leg doesn't count as transfer
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600),
            _walk(300),
            _trip("R1", "09:15:00", "10:00:00", 2700),
        ]
        assert _passes_filters(legs) is True


# ---------------------------------------------------------------------------
# _route_signature
# ---------------------------------------------------------------------------

class TestRouteSignature:
    def test_single_trip(self):
        legs = [_trip("R1", "08:00:00", "09:00:00", 3600, trip_id="T1")]
        assert _route_signature(legs) == ("T1",)

    def test_consecutive_same_trip_collapsed(self):
        # Two legs on the same trip_id → appears once in signature
        legs = [
            _trip("R1", "08:00:00", "08:30:00", 1800, trip_id="T1"),
            _trip("R1", "08:30:00", "09:00:00", 1800, trip_id="T1"),
        ]
        assert _route_signature(legs) == ("T1",)

    def test_two_different_trips(self):
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600, trip_id="T1"),
            _trip("R2", "09:30:00", "10:30:00", 3600, trip_id="T2"),
        ]
        assert _route_signature(legs) == ("T1", "T2")

    def test_walk_legs_included_in_signature(self):
        # Walk legs are included so routes with same trips but different transfers are distinct
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600, trip_id="T1"),
            _walk(300),
            _trip("R2", "09:30:00", "10:30:00", 3600, trip_id="T2"),
        ]
        assert _route_signature(legs) == ("T1", "walk:A:B", "T2")

    def test_walk_only_signature(self):
        assert _route_signature([_walk(300)]) == ("walk:A:B",)

    def test_different_walk_stops_produce_different_signatures(self):
        def _walk_custom(from_id: str, to_id: str) -> dict:
            return {
                "kind": "walk",
                "from_stop_id": from_id,
                "to_stop_id": to_id,
                "from_stop_name": from_id,
                "to_stop_name": to_id,
                "distance_m": 250.0,
                "walk_seconds": 300,
            }

        legs_a = [
            _trip("R1", "08:00:00", "09:00:00", 3600, trip_id="T1"),
            _walk_custom("B", "B1"),
            _trip("R2", "09:30:00", "10:30:00", 3600, trip_id="T2"),
        ]
        legs_b = [
            _trip("R1", "08:00:00", "09:00:00", 3600, trip_id="T1"),
            _walk_custom("B", "B2"),
            _trip("R2", "09:30:00", "10:30:00", 3600, trip_id="T2"),
        ]
        assert _route_signature(legs_a) != _route_signature(legs_b)

    def test_same_trip_ids_are_duplicates(self):
        # Two routes riding the same trips are equal even if stops differ
        route_a = [_trip("R1", "08:00:00", "09:00:00", 3600, trip_id="T1")]
        route_b = [
            _trip("R1", "08:00:00", "08:30:00", 1800, trip_id="T1"),
            _trip("R1", "08:30:00", "09:00:00", 1800, trip_id="T1"),
        ]
        assert _route_signature(route_a) == _route_signature(route_b)

    def test_different_trip_ids_are_not_duplicates(self):
        # Same route_id but different trip (later departure) → different signature
        route_early = [_trip("R1", "08:00:00", "09:00:00", 3600, trip_id="T_early")]
        route_late = [_trip("R1", "10:00:00", "11:00:00", 3600, trip_id="T_late")]
        assert _route_signature(route_early) != _route_signature(route_late)


# ---------------------------------------------------------------------------
# count_transfers
# ---------------------------------------------------------------------------

class TestCountTransfers:
    def test_empty_route(self):
        assert count_transfers([]) == 0

    def test_single_trip_leg(self):
        assert count_transfers([_trip("R1", "08:00:00", "09:00:00", 3600)]) == 0

    def test_same_route_two_legs(self):
        legs = [
            _trip("R1", "08:00:00", "08:30:00", 1800),
            _trip("R1", "08:30:00", "09:00:00", 1800),
        ]
        assert count_transfers(legs) == 0

    def test_one_transfer(self):
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600),
            _trip("R2", "09:30:00", "10:30:00", 3600),
        ]
        assert count_transfers(legs) == 1

    def test_two_transfers(self):
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600),
            _trip("R2", "09:30:00", "10:30:00", 3600),
            _trip("R3", "11:00:00", "12:00:00", 3600),
        ]
        assert count_transfers(legs) == 2

    def test_walk_legs_ignored(self):
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600),
            _walk(300),
            _trip("R2", "09:30:00", "10:30:00", 3600),
        ]
        assert count_transfers(legs) == 1

    def test_walk_only(self):
        assert count_transfers([_walk(300)]) == 0


# ---------------------------------------------------------------------------
# total_walk_metres
# ---------------------------------------------------------------------------

class TestTotalWalkMetres:
    def test_no_walk_legs(self):
        legs = [_trip("R1", "08:00:00", "09:00:00", 3600)]
        assert total_walk_metres(legs) == 0.0

    def test_single_walk_leg(self):
        leg = {**_walk(300), "distance_m": 400.0}
        assert total_walk_metres([leg]) == 400.0

    def test_multiple_walk_legs(self):
        legs = [
            {**_walk(300), "distance_m": 250.0},
            _trip("R1", "08:10:00", "09:00:00", 3000),
            {**_walk(120), "distance_m": 100.0},
        ]
        assert total_walk_metres(legs) == 350.0

    def test_empty_route(self):
        assert total_walk_metres([]) == 0.0


# ---------------------------------------------------------------------------
# _fill_later_departures
# ---------------------------------------------------------------------------

class TestFillLaterDepartures:
    """
    Unit tests for _fill_later_departures using a stub _schedule_path
    via monkeypatching.
    """

    def _make_route(self, trip_id: str, dep: str, arr: str, route_id: str = "R1") -> list:
        return [_trip(route_id, dep, arr, _hms_to_seconds(arr) - _hms_to_seconds(dep), trip_id=trip_id)]

    def test_no_fill_needed_when_full(self):
        """If routes already at max_routes, fill returns unchanged list."""
        import networkx as nx
        from unittest.mock import MagicMock
        from datetime import datetime

        routes = [
            self._make_route("T1", "08:00:00", "09:00:00"),
            self._make_route("T2", "10:00:00", "11:00:00"),
        ]
        seen = {("T1",), ("T2",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"], ["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=2,
        )
        assert len(result) == 2

    def test_fills_one_slot_with_later_departure(self, monkeypatch):
        """One existing route, max_routes=2: fill finds next departure."""
        import networkx as nx
        from unittest.mock import MagicMock
        from datetime import datetime
        import routing.engine as eng

        later_legs = self._make_route("T2", "10:00:00", "11:00:00")

        call_count = {"n": 0}
        def fake_schedule(session, G, node_path, dt, cache=None):
            call_count["n"] += 1
            if dt.hour >= 10:
                return None  # exhausted after T2
            return later_legs

        monkeypatch.setattr(eng, "_schedule_path", fake_schedule)
        monkeypatch.setattr(eng, "_passes_filters", lambda legs: True)

        routes = [self._make_route("T1", "08:00:00", "09:00:00")]
        seen = {("T1",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=2,
        )
        assert len(result) == 2
        assert ("T2",) in seen

    def test_skips_already_seen_signature(self, monkeypatch):
        """Fill skips a known sig and keeps advancing until exhausted."""
        import networkx as nx
        from unittest.mock import MagicMock
        from datetime import datetime
        import routing.engine as eng

        known_legs = self._make_route("T1", "10:00:00", "11:00:00")

        monkeypatch.setattr(eng, "_schedule_path", lambda *a, **kw: known_legs)
        monkeypatch.setattr(eng, "_passes_filters", lambda legs: False)  # force exhaustion

        routes = [self._make_route("T_orig", "08:00:00", "09:00:00")]
        seen = {("T_orig",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=2,
        )
        # Could not fill — path exhausted immediately
        assert len(result) == 1

    def test_exhausted_path_returns_none(self, monkeypatch):
        """If _schedule_path returns None immediately, no fill occurs."""
        import networkx as nx
        from unittest.mock import MagicMock
        from datetime import datetime
        import routing.engine as eng

        monkeypatch.setattr(eng, "_schedule_path", lambda *a, **kw: None)

        routes = [self._make_route("T1", "08:00:00", "09:00:00")]
        seen = {("T1",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=3,
        )
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _pick_longest_route
# ---------------------------------------------------------------------------

def _make_graph_with_routes(stop_pairs: list[tuple[str, str, str, float]]) -> nx.MultiDiGraph:
    """
    Build a MultiDiGraph from (u, v, route_id, weight) tuples.
    Each edge has kind="trip".
    """
    G = nx.MultiDiGraph()
    for u, v, route_id, weight in stop_pairs:
        for node in (u, v):
            if node not in G:
                G.add_node(node, name=f"Stop {node}")
        G.add_edge(u, v, route_id=route_id, weight=weight, kind="trip")
    return G


class TestPickLongestRoute:
    def test_single_candidate_returned_immediately(self):
        G = _make_graph_with_routes([("A", "B", "R1", 0)])
        assert _pick_longest_route(G, ["A", "B"], 0) == "R1"

    def test_longer_route_wins_over_shorter(self):
        # R1 covers A→B→C→D (3 hops), R2 covers only A→B (1 hop)
        edges = [
            ("A", "B", "R1", 0),
            ("B", "C", "R1", 0),
            ("C", "D", "R1", 0),
            ("A", "B", "R2", 0),  # same weight, but stops at B
        ]
        G = _make_graph_with_routes(edges)
        result = _pick_longest_route(G, ["A", "B", "C", "D"], 0)
        assert result == "R1"

    def test_tie_returns_one_of_the_candidates(self):
        # R1 and R2 both cover exactly A→B (same coverage)
        edges = [
            ("A", "B", "R1", 0),
            ("A", "B", "R2", 0),
        ]
        G = _make_graph_with_routes(edges)
        result = _pick_longest_route(G, ["A", "B"], 0)
        assert result in {"R1", "R2"}

    def test_look_ahead_from_non_zero_start(self):
        # Path is X→A→B→C→D; start=1 means we start from A→B
        # R1 covers A→B→C→D, R2 covers A→B only
        edges = [
            ("X", "A", "Rx", 0),
            ("A", "B", "R1", 0),
            ("B", "C", "R1", 0),
            ("C", "D", "R1", 0),
            ("A", "B", "R2", 0),
        ]
        G = _make_graph_with_routes(edges)
        result = _pick_longest_route(G, ["X", "A", "B", "C", "D"], 1)
        assert result == "R1"

    def test_only_min_weight_edges_are_candidates(self):
        # R1 has weight 10, R2 has weight 0 — only R2 qualifies despite shorter coverage
        G = nx.MultiDiGraph()
        for node in ("A", "B", "C"):
            G.add_node(node, name=f"Stop {node}")
        G.add_edge("A", "B", route_id="R1", weight=10, kind="trip")
        G.add_edge("B", "C", route_id="R1", weight=10, kind="trip")
        G.add_edge("A", "B", route_id="R2", weight=0, kind="trip")
        result = _pick_longest_route(G, ["A", "B", "C"], 0)
        assert result == "R2"


# ---------------------------------------------------------------------------
# _find_trip_legs
# ---------------------------------------------------------------------------

@pytest.fixture
def trip_db():
    """In-memory SQLite with a minimal GO Transit-like schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Stops
    for stop_id, name in [("S1", "Stop 1"), ("S2", "Stop 2"), ("S3", "Stop 3")]:
        session.add(Stop(stop_id=stop_id, stop_name=name, stop_lat=43.0, stop_lon=-79.0))

    # Route
    session.add(Route(route_id="R1", route_short_name="1", route_long_name="Test Route", route_type=3))

    # Trip running on 20260302
    session.add(Trip(trip_id="T1", route_id="R1", service_id="20260302", trip_headsign="Guelph", direction_id=0))

    # Stop times: S1 08:00, S2 08:30, S3 09:00
    session.add(StopTime(trip_id="T1", stop_id="S1", stop_sequence=1, departure_time="08:00:00", arrival_time="08:00:00"))
    session.add(StopTime(trip_id="T1", stop_id="S2", stop_sequence=2, departure_time="08:30:00", arrival_time="08:30:00"))
    session.add(StopTime(trip_id="T1", stop_id="S3", stop_sequence=3, departure_time="09:00:00", arrival_time="09:00:00"))

    session.commit()
    yield session
    session.close()
    engine.dispose()


def _make_trip_graph() -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for stop_id, name in [("S1", "Stop 1"), ("S2", "Stop 2"), ("S3", "Stop 3")]:
        G.add_node(stop_id, name=name)
    G.add_edge("S1", "S2", route_id="R1", weight=1800, kind="trip")
    G.add_edge("S2", "S3", route_id="R1", weight=1800, kind="trip")
    return G


class TestFindTripLegs:
    def test_happy_path_returns_legs(self, trip_db):
        G = _make_trip_graph()
        legs = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], 0, "20260302")
        assert legs is not None
        assert len(legs) == 2
        assert legs[0]["departure_time"] == "08:00:00"
        assert legs[0]["arrival_time"] == "08:30:00"
        assert legs[1]["departure_time"] == "08:30:00"
        assert legs[1]["arrival_time"] == "09:00:00"
        assert all(leg["trip_id"] == "T1" for leg in legs)
        assert all(leg["route_id"] == "R1" for leg in legs)

    def test_not_before_filters_early_departures(self, trip_db):
        G = _make_trip_graph()
        # Require departure at or after 08:30:01 — trip departs 08:00, should not match
        not_before = 8 * 3600 + 30 * 60 + 1
        legs = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], not_before, "20260302")
        assert legs is None

    def test_wrong_service_date_returns_none(self, trip_db):
        G = _make_trip_graph()
        legs = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], 0, "20260303")
        assert legs is None

    def test_stop_not_served_by_trip_returns_none(self, trip_db):
        # Add a stop the trip doesn't serve
        trip_db.add(Stop(stop_id="S9", stop_name="Unknown", stop_lat=43.0, stop_lon=-79.0))
        trip_db.commit()
        G = _make_trip_graph()
        G.add_node("S9", name="Unknown")
        legs = _find_trip_legs(trip_db, G, "R1", ["S1", "S9"], 0, "20260302")
        assert legs is None

    def test_service_calendar_exception_type_2_blocks_trip(self, trip_db):
        # Add a removal exception for the trip's service_id on the travel date
        trip_db.add(ServiceCalendarDate(service_id="20260302", date="20260302", exception_type=2))
        trip_db.commit()
        G = _make_trip_graph()
        legs = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], 0, "20260302")
        assert legs is None

    def test_service_calendar_exception_type_1_does_not_block(self, trip_db):
        # exception_type=1 means service added — should still return legs
        trip_db.add(ServiceCalendarDate(service_id="20260302", date="20260302", exception_type=1))
        trip_db.commit()
        G = _make_trip_graph()
        legs = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], 0, "20260302")
        assert legs is not None

    def test_cache_hit_reuses_trip_id(self, trip_db):
        G = _make_trip_graph()
        cache = _RouteQueryCache()
        # First call populates cache
        legs1 = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], 0, "20260302", cache)
        # Manually corrupt the DB trip to verify second call uses cache, not DB
        trip_db.execute(__import__("sqlalchemy").text("UPDATE trips SET route_id='GONE' WHERE trip_id='T1'"))
        legs2 = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], 0, "20260302", cache)
        assert legs1 is not None
        assert legs2 is not None
        # Both calls should produce identical legs since cache replays trip_id
        assert [l["trip_id"] for l in legs1] == [l["trip_id"] for l in legs2]

    def test_single_stop_segment_returns_none(self, trip_db):
        # When stops has one element, first_stop == last_stop. The SQL requires
        # st_last.stop_sequence > st_first.stop_sequence for the same stop_id,
        # which a non-circular trip cannot satisfy, so the result is None.
        G = _make_trip_graph()
        legs = _find_trip_legs(trip_db, G, "R1", ["S1"], 0, "20260302")
        assert legs is None

    def test_schedule_path_treats_empty_legs_as_no_route(self, trip_db):
        # _find_trip_legs can theoretically return [] (empty, not None) for a
        # degenerate single-stop segment on a circular trip. _schedule_path must
        # handle this without raising IndexError on trip_legs[-1].
        import routing.engine as eng
        from unittest.mock import patch
        from datetime import datetime

        G = _make_trip_graph()

        with patch.object(eng, "_find_trip_legs", return_value=[]):
            result = eng._schedule_path(trip_db, G, ["S1", "S2"], datetime(2026, 3, 2, 8, 0, 0))
        assert result is None
