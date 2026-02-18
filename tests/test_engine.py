"""
Unit tests for routing.engine pure functions.

These tests have no DB or graph dependency — they exercise only the
logic that lives entirely inside routing/engine.py.
"""

import pytest

from config import MAX_TRANSFERS, MIN_TRANSFER_MINUTES
from routing.engine import (
    _fill_later_departures,
    _hms_to_seconds,
    _passes_filters,
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

    def test_walk_legs_excluded(self):
        legs = [
            _trip("R1", "08:00:00", "09:00:00", 3600, trip_id="T1"),
            _walk(300),
            _trip("R2", "09:30:00", "10:30:00", 3600, trip_id="T2"),
        ]
        assert _route_signature(legs) == ("T1", "T2")

    def test_walk_only_is_empty(self):
        assert _route_signature([_walk(300)]) == ()

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
        def fake_schedule(session, G, node_path, dt):
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
