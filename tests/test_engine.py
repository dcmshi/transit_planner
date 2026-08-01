"""
Unit tests for routing.engine pure functions.

These tests have no DB or graph dependency — they exercise only the
logic that lives entirely inside routing/engine.py.
"""

from collections.abc import Sequence
from datetime import date, datetime
from typing import cast
from unittest.mock import MagicMock, patch

import networkx as nx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from config import MIN_TRANSFER_MINUTES
from db.models import Base, Route, ServiceCalendarDate, Stop, StopTime, Trip
from routing.engine import (
    ARRIVE_BY_LOOKBACK_HOURS,
    _fill_later_departures,
    _find_trip_legs,
    _hms_to_seconds,
    _keep_if_not_dominated,
    _leg_geometry,
    _passes_filters,
    _pick_longest_route,
    _route_signature,
    _RouteQueryCache,
    _schedule_path,
    _TripShape,
    count_transfers,
    dominates,
    encode_polyline,
    find_routes_arriving_by,
    route_metrics,
    total_travel_seconds,
    total_walk_metres,
)
from routing.engine import Route as RouteLegs  # db.models.Route is the GTFS model

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
# find_routes — disconnected stops
# ---------------------------------------------------------------------------

class TestFindRoutesNoPath:
    def test_disconnected_stops_return_empty_list(self):
        """Both stops exist but nothing connects them: find_routes returns []
        (the API turns that into a 404) instead of leaking NetworkXNoPath
        (which the API turned into a 500)."""
        from datetime import datetime

        import graph.builder as builder_mod
        from routing.engine import find_routes

        G: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        G.add_node("A", name="Stop A", lat=43.0, lon=-79.0)
        G.add_node("B", name="Stop B", lat=43.1, lon=-79.1)  # no edges
        H: nx.DiGraph[str] = nx.DiGraph()
        H.add_nodes_from(G.nodes(data=True))

        old = builder_mod._graphs
        builder_mod._graphs = (G, H)
        try:
            # No path exists, so find_routes returns before touching the session.
            routes = find_routes(
                "A", "B", departure_dt=datetime(2026, 2, 11, 8, 0),
                session=cast(Session, None),
            )
        finally:
            builder_mod._graphs = old

        assert routes == []


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
        # The module docstring used to claim these were filtered out; on the
        # current GO feed 353 of 1,927 trip edges (18%) are zero-second, so
        # "restoring" that filter would delete a fifth of the network.
        legs = [_trip("R1", "08:00:00", "08:00:00", 0)]
        assert _passes_filters(legs) is True

    def test_run_of_zero_second_legs_passes(self):
        """Closely-spaced stops produce several in a row, which is the shape
        the real feed actually yields."""
        legs = [
            _trip("R1", "08:00:00", "08:00:00", 0),
            _trip("R1", "08:00:00", "08:00:00", 0),
            _trip("R1", "08:00:00", "08:05:00", 300),
        ]
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
        from datetime import datetime
        from unittest.mock import MagicMock

        import networkx as nx

        routes = [
            self._make_route("T1", "08:00:00", "09:00:00"),
            self._make_route("T2", "10:00:00", "11:00:00"),
        ]
        seen: set[tuple[str, ...]] = {("T1",), ("T2",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"], ["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=2,
        )
        assert len(result) == 2

    def test_fills_one_slot_with_later_departure(self, monkeypatch):
        """One existing route, max_routes=2: fill finds next departure."""
        from datetime import datetime
        from unittest.mock import MagicMock

        import networkx as nx

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
        seen: set[tuple[str, ...]] = {("T1",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=2,
        )
        assert len(result) == 2
        assert ("T2",) in seen

    def test_skips_already_seen_signature(self, monkeypatch):
        """Fill skips a known sig but keeps advancing to the next departure."""
        from datetime import datetime
        from unittest.mock import MagicMock

        import networkx as nx

        import routing.engine as eng

        seen_legs = self._make_route("T2", "10:00:00", "11:00:00")
        fresh_legs = self._make_route("T3", "11:00:00", "12:00:00")

        def fake_schedule(session, G, node_path, dt, cache=None):
            if dt.hour < 10 or (dt.hour == 10 and dt.minute == 0 and dt.second <= 1):
                return seen_legs
            if dt.hour < 11 or (dt.hour == 11 and dt.minute == 0 and dt.second <= 1):
                return fresh_legs
            return None

        monkeypatch.setattr(eng, "_schedule_path", fake_schedule)
        monkeypatch.setattr(eng, "_passes_filters", lambda legs: True)

        routes = [self._make_route("T_orig", "08:00:00", "09:00:00")]
        seen: set[tuple[str, ...]] = {("T_orig",), ("T2",)}  # T2's signature already known
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=2,
        )
        # T2 skipped as duplicate, pointer advanced, T3 filled the slot.
        assert len(result) == 2
        assert ("T3",) in seen

    def test_filter_failure_advances_to_next_departure(self, monkeypatch):
        """A departure failing filters must not exhaust the path —
        the next departure on the same path is still tried (regression)."""
        from datetime import datetime
        from unittest.mock import MagicMock

        import networkx as nx

        import routing.engine as eng

        bad_legs = self._make_route("T_bad", "09:00:00", "10:00:00")
        good_legs = self._make_route("T_good", "10:00:00", "11:00:00")

        def fake_schedule(session, G, node_path, dt, cache=None):
            if dt.hour < 9 or (dt.hour == 9 and dt.minute == 0 and dt.second <= 1):
                return bad_legs
            if dt.hour < 10 or (dt.hour == 10 and dt.minute == 0 and dt.second <= 1):
                return good_legs
            return None

        monkeypatch.setattr(eng, "_schedule_path", fake_schedule)
        monkeypatch.setattr(
            eng, "_passes_filters", lambda legs: legs[0]["trip_id"] != "T_bad"
        )

        routes = [self._make_route("T1", "08:00:00", "09:00:00")]
        seen: set[tuple[str, ...]] = {("T1",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=2,
        )
        assert len(result) == 2
        assert ("T_good",) in seen
        assert ("T_bad",) not in seen

    def test_all_departures_filtered_terminates_without_fill(self, monkeypatch):
        """If every remaining departure fails filters, fill terminates once
        the timetable is exhausted and adds nothing."""
        from datetime import datetime
        from unittest.mock import MagicMock

        import networkx as nx

        import routing.engine as eng

        bad_legs = self._make_route("T_bad", "10:00:00", "11:00:00")

        def fake_schedule(session, G, node_path, dt, cache=None):
            if dt.hour < 10 or (dt.hour == 10 and dt.minute == 0 and dt.second <= 1):
                return bad_legs
            return None  # timetable exhausted

        monkeypatch.setattr(eng, "_schedule_path", fake_schedule)
        monkeypatch.setattr(eng, "_passes_filters", lambda legs: False)

        routes = [self._make_route("T_orig", "08:00:00", "09:00:00")]
        seen: set[tuple[str, ...]] = {("T_orig",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=2,
        )
        assert len(result) == 1

    def test_exhausted_path_returns_none(self, monkeypatch):
        """If _schedule_path returns None immediately, no fill occurs."""
        from datetime import datetime
        from unittest.mock import MagicMock

        import networkx as nx

        import routing.engine as eng

        monkeypatch.setattr(eng, "_schedule_path", lambda *a, **kw: None)

        routes = [self._make_route("T1", "08:00:00", "09:00:00")]
        seen: set[tuple[str, ...]] = {("T1",)}
        result = _fill_later_departures(
            MagicMock(), nx.MultiDiGraph(),
            routes, [["A", "B"]],
            seen, datetime(2026, 2, 17, 8, 0, 0), max_routes=3,
        )
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _pick_longest_route
# ---------------------------------------------------------------------------

def _make_graph_with_routes(
    stop_pairs: Sequence[tuple[str, str, str, float]],
) -> nx.MultiDiGraph:
    """
    Build a MultiDiGraph from (u, v, route_id, weight) tuples.
    Each edge has kind="trip".
    """
    G: nx.MultiDiGraph[str] = nx.MultiDiGraph()
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

    def test_coverage_tie_broken_by_segment_weight_not_first_hop(self):
        """Ninth-pass fix: with equal coverage, the route faster over the
        WHOLE segment ranks first — first-hop weight alone let a slower
        route shadow a faster one."""
        from routing.engine import _rank_routes_by_coverage

        G: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        for node in ("A", "B", "C"):
            G.add_node(node, name=f"Stop {node}")
        # R_slow wins hop 1 (100 vs 200) but loses the segment (100+900
        # vs 200+300).
        G.add_edge("A", "B", route_id="R_slow", weight=100, kind="trip")
        G.add_edge("B", "C", route_id="R_slow", weight=900, kind="trip")
        G.add_edge("A", "B", route_id="R_fast", weight=200, kind="trip")
        G.add_edge("B", "C", route_id="R_fast", weight=300, kind="trip")

        assert _rank_routes_by_coverage(G, ["A", "B", "C"], 0) == ["R_fast", "R_slow"]

    def test_coverage_beats_weight_and_all_routes_are_candidates(self):
        """All trip routes on the segment are candidates (not just those
        tied at the minimum weight — see the 2026-07-10 eighth-pass fix),
        ranked by corridor coverage first, then weight."""
        from routing.engine import _rank_routes_by_coverage

        G: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        for node in ("A", "B", "C"):
            G.add_node(node, name=f"Stop {node}")
        G.add_edge("A", "B", route_id="R1", weight=10, kind="trip")
        G.add_edge("B", "C", route_id="R1", weight=10, kind="trip")
        G.add_edge("A", "B", route_id="R2", weight=0, kind="trip")

        # R1 covers both pairs (despite the higher weight); R2 terminates
        # after one pair but stays in the ranking as a fallback.
        assert _rank_routes_by_coverage(G, ["A", "B", "C"], 0) == ["R1", "R2"]
        assert _pick_longest_route(G, ["A", "B", "C"], 0) == "R1"


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
    G: nx.MultiDiGraph[str] = nx.MultiDiGraph()
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
        assert [leg["trip_id"] for leg in legs1] == [leg["trip_id"] for leg in legs2]

    def test_single_stop_segment_returns_none(self, trip_db):
        # When stops has one element, first_stop == last_stop. The SQL requires
        # st_last.stop_sequence > st_first.stop_sequence for the same stop_id,
        # which a non-circular trip cannot satisfy, so the result is None.
        G = _make_trip_graph()
        legs = _find_trip_legs(trip_db, G, "R1", ["S1"], 0, "20260302")
        assert legs is None

    def test_schedule_path_falls_back_to_route_with_service(self, trip_db):
        """Two route_ids share the corridor (GTFS publishes one per schedule
        period); the candidate ranked first has no trips on the travel date.
        _schedule_path must fall back to the other candidate instead of
        returning None (regression — found live with the June 2026 feed)."""
        from datetime import datetime

        import routing.engine as eng

        # "00-FUTURE" sorts before "R1" on the coverage tie-break, so it is
        # tried first — and has no trips on 20260302.
        trip_db.add(Route(route_id="00-FUTURE", route_short_name="1",
                          route_long_name="Next period", route_type=3))
        trip_db.commit()

        G = _make_trip_graph()
        G.add_edge("S1", "S2", route_id="00-FUTURE", weight=1800, kind="trip")
        G.add_edge("S2", "S3", route_id="00-FUTURE", weight=1800, kind="trip")

        assert eng._rank_routes_by_coverage(G, ["S1", "S2", "S3"], 0) == ["00-FUTURE", "R1"]

        legs = eng._schedule_path(trip_db, G, ["S1", "S2", "S3"], datetime(2026, 3, 2, 7, 0, 0))
        assert legs is not None
        assert all(leg["route_id"] == "R1" for leg in legs)

    def test_slower_route_tried_when_faster_has_no_service(self, trip_db):
        """Regression: only routes tied at the minimum edge weight were
        candidates, so a schedule period with even slightly different run
        times (weight 600 vs 900) was the sole candidate — and when it had
        no trips on the travel date, the whole path died despite a valid
        slower alternative."""
        from datetime import datetime

        import routing.engine as eng

        trip_db.add(Route(route_id="R_fast", route_short_name="9",
                          route_long_name="No service today", route_type=3))
        trip_db.commit()

        G = _make_trip_graph()  # R1 edges at weight 1800, has service
        G.add_edge("S1", "S2", route_id="R_fast", weight=600, kind="trip")
        G.add_edge("S2", "S3", route_id="R_fast", weight=600, kind="trip")

        ranked = eng._rank_routes_by_coverage(G, ["S1", "S2", "S3"], 0)
        assert ranked == ["R_fast", "R1"]  # both candidates, fastest first

        legs = eng._schedule_path(trip_db, G, ["S1", "S2", "S3"],
                                  datetime(2026, 3, 2, 7, 0, 0))
        assert legs is not None
        assert all(leg["route_id"] == "R1" for leg in legs)

    def test_express_variant_falls_through_to_local(self, trip_db):
        """Regression: _find_trip_legs selected the single earliest trip
        and gave up if it skipped an intermediate stop — an express leaving
        just before a valid local killed every local-stop itinerary."""
        # Express T_exp at 07:30 serves S1 and S3 only (skips S2).
        trip_db.add(Trip(trip_id="T_exp", route_id="R1", service_id="20260302",
                         trip_headsign="Express", direction_id=0))
        trip_db.add(StopTime(trip_id="T_exp", stop_id="S1", stop_sequence=1,
                             departure_time="07:30:00", arrival_time="07:30:00"))
        trip_db.add(StopTime(trip_id="T_exp", stop_id="S3", stop_sequence=2,
                             departure_time="08:15:00", arrival_time="08:15:00"))
        trip_db.commit()

        G = _make_trip_graph()
        # T1 (the 08:00 local serving S1,S2,S3) must be found even though
        # the 07:30 express matches the first/last-stop query first.
        legs = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], 0, "20260302")

        assert legs is not None
        assert all(leg["trip_id"] == "T1" for leg in legs)
        assert legs[0]["departure_time"] == "08:00:00"

    def test_schedule_path_treats_empty_legs_as_no_route(self, trip_db):
        # _find_trip_legs can theoretically return [] (empty, not None) for a
        # degenerate single-stop segment on a circular trip. _schedule_path must
        # handle this without raising IndexError on trip_legs[-1].
        from datetime import datetime
        from unittest.mock import patch

        import routing.engine as eng

        G = _make_trip_graph()

        with patch.object(eng, "_find_trip_legs", return_value=[]):
            result = eng._schedule_path(trip_db, G, ["S1", "S2"], datetime(2026, 3, 2, 8, 0, 0))
        assert result is None


# ---------------------------------------------------------------------------
# find_routes_arriving_by
# ---------------------------------------------------------------------------

class TestFindRoutesArrivingBy:
    """Yen's is departure-anchored, so arrive-by searches forward from a
    window before the deadline and keeps what still makes it."""

    def _legs(self, dep, arr):
        # Distinct trip_id per departure: results are deduplicated by route
        # signature across widened passes, and real departures are different
        # trips.  Sharing one id collapses every fixture into a single result.
        return [_trip("R1", dep, arr, _hms_to_seconds(arr) - _hms_to_seconds(dep),
                      trip_id=f"T{dep}")]

    def _patched(self, candidates):
        return patch("routing.engine.find_routes", return_value=candidates)

    def test_drops_itineraries_that_arrive_too_late(self):
        candidates = [
            self._legs("08:00:00", "08:50:00"),   # in time
            self._legs("08:30:00", "09:30:00"),   # too late
        ]
        with self._patched(candidates):
            result = find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("09:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            ).routes
        assert [r[0]["arrival_time"] for r in result] == ["08:50:00"]

    def test_exactly_on_the_deadline_is_accepted(self):
        with self._patched([self._legs("08:00:00", "09:00:00")]):
            result = find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("09:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            ).routes
        assert len(result) == 1

    def test_orders_latest_departure_first(self):
        """Both arrive in time, so the rider wants the later start."""
        candidates = [
            self._legs("07:00:00", "08:40:00"),
            self._legs("08:00:00", "08:50:00"),
            self._legs("07:30:00", "08:45:00"),
        ]
        with self._patched(candidates):
            result = find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("09:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            ).routes
        assert [r[0]["departure_time"] for r in result] == [
            "08:00:00", "07:30:00", "07:00:00",
        ]

    def test_searches_from_the_lookback_window(self):
        with patch("routing.engine.find_routes", return_value=[]) as mock_find:
            find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("12:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            )
        # The first pass uses the initial window; later passes widen it.
        first_call = mock_find.call_args_list[0].kwargs["departure_dt"]
        assert first_call == datetime(2026, 2, 17, 12 - ARRIVE_BY_LOOKBACK_HOURS, 0, 0)

    def test_early_deadline_clamps_the_window_to_midnight(self):
        with patch("routing.engine.find_routes", return_value=[]) as mock_find:
            find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("02:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            )
        assert mock_find.call_args.kwargs["departure_dt"] == datetime(2026, 2, 17, 0, 0, 0)

    def test_post_midnight_deadline_is_expressible(self):
        """25:30 is 01:30 the next morning — the reason this takes seconds
        rather than a datetime, which cannot hold hour 25."""
        candidates = [self._legs("23:00:00", "25:00:00")]
        with self._patched(candidates):
            result = find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("25:30:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            ).routes
        assert len(result) == 1
        # The window still starts inside the travel day.
        assert result[0][0]["departure_time"] == "23:00:00"

    def test_respects_max_routes(self):
        candidates = [self._legs(f"0{h}:00:00", "08:55:00") for h in range(5, 9)]
        with self._patched(candidates):
            result = find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("09:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(), max_routes=2,
            ).routes
        assert len(result) == 2

    def test_window_widens_until_it_finds_service(self):
        """Regression: a fixed four-hour lookback returned nothing whenever the
        last departure fell outside it.  GL->UN runs three trains a day, so
        arrive_by=23:59 saw an empty window and 404'd even though every one of
        those trains arrives before the deadline."""
        early = [self._legs("08:08:00", "09:35:00")]
        calls = []

        def widening(*args, **kwargs):
            calls.append(kwargs["departure_dt"])
            # Only visible once the window reaches back past 08:08.
            return early if kwargs["departure_dt"].hour <= 8 else []

        with patch("routing.engine.find_routes", side_effect=widening):
            result = find_routes_arriving_by(
                "GL", "UN", arrive_by_sec=_hms_to_seconds("23:59:00"),
                travel_day=date(2026, 8, 3), session=MagicMock(),
            )

        assert len(calls) > 1, "should have widened past the initial window"
        assert [r[0]["departure_time"] for r in result.routes] == ["08:08:00"]

    def test_widening_stops_once_enough_is_found(self):
        """Frequent service must not be widened into: a day-wide search would
        surface the earliest departures when the caller wants the latest."""
        plenty = [self._legs(f"{h:02d}:47:00", f"{h + 1:02d}:35:00") for h in range(17, 23)]
        calls = []

        def counting(*args, **kwargs):
            calls.append(kwargs["departure_dt"])
            return plenty

        with patch("routing.engine.find_routes", side_effect=counting):
            find_routes_arriving_by(
                "MO", "UN", arrive_by_sec=_hms_to_seconds("23:59:00"),
                travel_day=date(2026, 8, 3), session=MagicMock(),
            )

        assert len(calls) == 1

    def test_reports_service_exists_when_none_makes_the_deadline(self):
        """Lets the API say "try a later deadline" instead of "no such route"."""
        late = [self._legs("20:00:00", "21:00:00")]
        with patch("routing.engine.find_routes", return_value=late):
            result = find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("09:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            )
        assert result.routes == []
        assert result.any_service is True

    def test_reports_no_service_when_nothing_is_found_at_all(self):
        with patch("routing.engine.find_routes", return_value=[]):
            result = find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("09:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            )
        assert result.routes == []
        assert result.any_service is False

    def test_walk_only_candidates_are_skipped(self):
        with self._patched([[_walk(300)]]):
            result = find_routes_arriving_by(
                "A", "B", arrive_by_sec=_hms_to_seconds("09:00:00"),
                travel_day=date(2026, 2, 17), session=MagicMock(),
            ).routes
        assert result == []


# ---------------------------------------------------------------------------
# Dominance
# ---------------------------------------------------------------------------

class TestDominates:
    """Every axis reads lower-is-better; departure is pre-negated by
    route_metrics so that leaving later counts as better."""

    def test_better_on_one_axis_equal_on_rest(self):
        assert dominates((0, 100, 0, 0.0), (0, 200, 0, 0.0)) is True

    def test_identical_does_not_dominate(self):
        assert dominates((0, 100, 0, 0.0), (0, 100, 0, 0.0)) is False

    def test_mixed_wins_and_losses_does_not_dominate(self):
        # Arrives earlier but walks further — a real trade-off, keep both.
        assert dominates((0, 100, 0, 500.0), (0, 200, 0, 0.0)) is False
        assert dominates((0, 200, 0, 0.0), (0, 100, 0, 500.0)) is False

    def test_worse_on_one_axis_does_not_dominate(self):
        assert dominates((0, 200, 0, 0.0), (0, 100, 0, 0.0)) is False

    def test_later_departure_dominates_when_arrival_matches(self):
        later = route_metrics([_trip("R1", "09:00:00", "10:00:00", 3600)])
        earlier = route_metrics([_trip("R1", "08:00:00", "10:00:00", 7200)])
        assert later is not None and earlier is not None
        assert dominates(later, earlier) is True
        assert dominates(earlier, later) is False


class TestRouteMetrics:
    def test_none_without_trip_legs(self):
        assert route_metrics([_walk(300)]) is None

    def test_departure_is_negated(self):
        m = route_metrics([_trip("R1", "08:00:00", "09:00:00", 3600)])
        assert m is not None
        assert m[0] == -_hms_to_seconds("08:00:00")
        assert m[1] == _hms_to_seconds("09:00:00")

    def test_walk_override_beats_the_legs(self):
        """A scored route carries its own total; that value, not a second
        derivation from the legs, is what the API has always compared on."""
        legs = [_trip("R1", "08:00:00", "09:00:00", 3600)]
        from_legs, overridden = route_metrics(legs), route_metrics(legs, 450.0)
        assert from_legs is not None and overridden is not None
        assert from_legs[3] == 0.0
        assert overridden[3] == 450.0


class TestKeepIfNotDominated:
    """The engine's route budget must not be spent on itineraries the API
    would delete. A fourteen-hour wait at an interchange passes every hard
    filter, so before this it occupied a slot until _prune_dominated ran."""

    def _route(self, dep, arr):
        return [_trip("R1", dep, arr, _hms_to_seconds(arr) - _hms_to_seconds(dep))]

    def test_first_route_is_always_kept(self):
        routes: list[RouteLegs] = []
        assert _keep_if_not_dominated(routes, self._route("08:00:00", "09:00:00")) is True
        assert len(routes) == 1

    def test_dominated_candidate_is_rejected(self):
        routes = [self._route("08:00:00", "09:00:00")]
        # Same departure, arrives 14 hours later — the shape that wasted slots.
        assert _keep_if_not_dominated(routes, self._route("08:00:00", "23:00:00")) is False
        assert len(routes) == 1

    def test_candidate_evicts_the_route_it_dominates(self):
        routes = [self._route("08:00:00", "23:00:00")]
        assert _keep_if_not_dominated(routes, self._route("08:00:00", "09:00:00")) is True
        assert len(routes) == 1
        assert routes[0][0]["arrival_time"] == "09:00:00"

    def test_incomparable_routes_are_both_kept(self):
        routes = [self._route("08:00:00", "09:00:00")]
        # Leaves later but arrives later too.
        assert _keep_if_not_dominated(routes, self._route("08:30:00", "09:30:00")) is True
        assert len(routes) == 2

    def test_walk_only_candidate_is_rejected(self):
        routes: list[RouteLegs] = []
        assert _keep_if_not_dominated(routes, [_walk(300)]) is False
        assert routes == []

    def test_paths_stay_index_aligned_through_eviction(self):
        """_fill_later_departures seeds path_not_before from routes but indexes
        it by candidate_paths, so routes[i] must keep describing paths[i]."""
        routes = [self._route("08:00:00", "23:00:00"), self._route("09:00:00", "09:30:00")]
        paths = [["A", "SLOW", "B"], ["A", "FAST", "B"]]

        added = _keep_if_not_dominated(
            routes, self._route("08:00:00", "09:00:00"), paths, ["A", "NEW", "B"]
        )

        assert added is True
        assert len(routes) == len(paths)
        # The 23:00 route was evicted; its path went with it.
        assert paths == [["A", "FAST", "B"], ["A", "NEW", "B"]]
        assert [r[0]["arrival_time"] for r in routes] == ["09:30:00", "09:00:00"]

    def test_rejected_candidate_leaves_paths_untouched(self):
        routes = [self._route("08:00:00", "09:00:00")]
        paths = [["A", "B"]]
        assert _keep_if_not_dominated(
            routes, self._route("08:00:00", "23:00:00"), paths, ["A", "C", "B"]
        ) is False
        assert paths == [["A", "B"]]


# ---------------------------------------------------------------------------
# Leg coordinates
# ---------------------------------------------------------------------------

class TestLegCoordinates:
    """Legs carry their stops' coordinates so a map client can draw a route
    without resolving every intermediate stop through /stops."""

    def _graph(self):
        G: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        G.add_node("A", name="Stop A", lat=43.5443, lon=-80.2469)
        G.add_node("B", name="Stop B", lat=43.6749, lon=-79.8221)
        return G

    def test_walk_leg_carries_both_endpoints(self):
        G = self._graph()
        G.add_edge("A", "B", kind="walk", distance_m=250.0, walk_seconds=200, weight=200)

        legs = _schedule_path(MagicMock(), G, ["A", "B"], datetime(2026, 2, 17, 8, 0))

        assert legs is not None
        leg = legs[0]
        assert (leg["from_lat"], leg["from_lon"]) == (43.5443, -80.2469)
        assert (leg["to_lat"], leg["to_lon"]) == (43.6749, -79.8221)

    def test_trip_leg_carries_both_endpoints(self, trip_db):
        G = _make_trip_graph()
        for stop_id, lat, lon in [("S1", 43.0, -79.0), ("S2", 43.1, -79.1),
                                  ("S3", 43.2, -79.2)]:
            G.nodes[stop_id]["lat"] = lat
            G.nodes[stop_id]["lon"] = lon

        legs = _find_trip_legs(trip_db, G, "R1", ["S1", "S2", "S3"], 0, "20260302")

        assert legs is not None
        assert (legs[0]["from_lat"], legs[0]["from_lon"]) == (43.0, -79.0)
        assert (legs[0]["to_lat"], legs[0]["to_lon"]) == (43.1, -79.1)
        assert (legs[1]["from_lat"], legs[1]["from_lon"]) == (43.1, -79.1)
        assert (legs[1]["to_lat"], legs[1]["to_lon"]) == (43.2, -79.2)

    def test_missing_node_coordinates_become_none(self):
        """A node without lat/lon yields nulls rather than raising — the same
        defensive shape the stop-name fallback already uses."""
        G: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        G.add_node("A", name="Stop A")  # no lat/lon
        G.add_node("B", name="Stop B", lat=43.6, lon=-79.8)
        G.add_edge("A", "B", kind="walk", distance_m=100.0, walk_seconds=80, weight=80)

        legs = _schedule_path(MagicMock(), G, ["A", "B"], datetime(2026, 2, 17, 8, 0))

        assert legs is not None
        assert legs[0]["from_lat"] is None
        assert legs[0]["to_lat"] == 43.6


# ---------------------------------------------------------------------------
# Leg track geometry
# ---------------------------------------------------------------------------

def _straight_shape(n=11):
    """West-to-east polyline at constant latitude, 0.01 deg per step."""
    return _TripShape(
        points=[[-79.0 + i * 0.01, 43.0] for i in range(n)],
        stop_indices={"S1": 0, "S2": 5, "S3": 10},
    )


def _decode_polyline(encoded, precision=5):
    """Independent decoder, so the tests below check the encoder rather than
    just agreeing with it.  Returns [lon, lat] pairs (GeoJSON order)."""
    coords, index, lat, lon = [], 0, 0, 0
    factor = 10 ** precision
    while index < len(encoded):
        for axis in range(2):
            result, shift = 0, 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        coords.append([lon / factor, lat / factor])
    return coords


def _flat(points):
    """pytest.approx cannot compare nested sequences."""
    return [c for point in points for c in point]


class TestEncodePolyline:
    def test_matches_the_reference_vector(self):
        """Google's published example, so the format is verified against the
        spec rather than against this implementation."""
        points = [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]]
        assert encode_polyline(points) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"

    def test_round_trips(self):
        points = [[-79.3806, 43.6453], [-79.9, 43.65], [-80.2469, 43.5443]]
        decoded = _decode_polyline(encode_polyline(points))
        assert _flat(decoded) == pytest.approx(_flat(points), abs=1e-5)

    def test_empty_input(self):
        assert encode_polyline([]) == ""

    def test_precision_is_about_a_metre(self):
        """Precision 5 must be finer than the simplification already applied,
        so encoding adds no visible error."""
        points = [[-79.380612, 43.645341], [-79.380699, 43.645388]]
        decoded = _decode_polyline(encode_polyline(points))
        for got, want in zip(decoded, points):
            assert abs(got[0] - want[0]) < 1e-5
            assert abs(got[1] - want[1]) < 1e-5


class TestLegGeometry:
    """A leg is a slice of its trip's shape, per leg so the map can colour
    each one by its own risk label.  Encoded as a polyline string."""

    def _points(self, shape, a, b):
        encoded = _leg_geometry(shape, a, b)
        return None if encoded is None else _decode_polyline(encoded)

    def test_slice_covers_only_the_leg(self):
        geom = self._points(_straight_shape(), "S1", "S2")
        assert geom is not None
        assert _flat(geom[:1]) == pytest.approx([-79.0, 43.0], abs=1e-5)
        assert _flat(geom[-1:]) == pytest.approx([-78.95, 43.0], abs=1e-5)

    def test_consecutive_legs_join_up(self):
        """The map draws legs separately; a gap between them would show."""
        first = self._points(_straight_shape(), "S1", "S2")
        second = self._points(_straight_shape(), "S2", "S3")
        assert first is not None and second is not None
        assert _flat(first[-1:]) == pytest.approx(_flat(second[:1]), abs=1e-5)

    def test_reversed_stop_order_returns_reversed_geometry(self):
        forward = self._points(_straight_shape(), "S1", "S2")
        backward = self._points(_straight_shape(), "S2", "S1")
        assert forward is not None and backward is not None
        assert _flat(backward) == pytest.approx(_flat(forward[::-1]), abs=1e-5)

    def test_no_shape_gives_none(self):
        assert _leg_geometry(None, "S1", "S2") is None

    def test_stop_absent_from_shape_gives_none(self):
        assert _leg_geometry(_straight_shape(), "S1", "NOT_ON_SHAPE") is None

    def test_zero_length_slice_gives_none(self):
        """Two stops projecting onto the same vertex cannot make a line."""
        shape = _TripShape(points=[[-79.0, 43.0], [-78.99, 43.0]],
                           stop_indices={"A": 0, "B": 0})
        assert _leg_geometry(shape, "A", "B") is None

    def test_geometry_is_simplified(self):
        """Douglas-Peucker collapses collinear points; a straight run of 101
        vertices carries no more information than its endpoints."""
        shape = _TripShape(
            points=[[-79.0 + i * 0.001, 43.0] for i in range(101)],
            stop_indices={"A": 0, "B": 100},
        )
        geom = self._points(shape, "A", "B")
        assert geom is not None
        assert len(geom) == 2

    def test_curvature_is_preserved(self):
        """Simplification must not straighten a genuine bend."""
        points = [[-79.0, 43.0], [-78.99, 43.0], [-78.98, 43.05], [-78.97, 43.0]]
        shape = _TripShape(points=points, stop_indices={"A": 0, "B": 3})
        geom = self._points(shape, "A", "B")
        assert geom is not None
        assert any(_flat([p]) == pytest.approx([-78.98, 43.05], abs=1e-5) for p in geom)

    def test_decodes_to_lon_lat(self):
        geom = self._points(_straight_shape(), "S1", "S2")
        assert geom is not None
        lon, lat = geom[0]
        assert -80 < lon < -78 and 42 < lat < 44

    def test_encoding_is_much_smaller_than_raw_pairs(self):
        """The reason for the format change.  ~4.9x on a 120-point line at
        these coordinate magnitudes; assert a conservative 3x so the test
        tracks the property rather than one measurement."""
        import json

        points = [[-79.0 + i * 0.0007, 43.0 + (i % 7) * 0.0009] for i in range(120)]
        encoded = encode_polyline(points)
        raw = json.dumps([[round(x, 6), round(y, 6)] for x, y in points])
        assert len(encoded) < len(raw) / 3
