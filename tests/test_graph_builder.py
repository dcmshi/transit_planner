"""
Unit tests for graph.builder pure functions.

_haversine_metres and _hms_to_seconds are private helpers but are
tested directly since they encapsulate meaningful logic.
"""

import pytest
import networkx as nx
from graph.builder import _haversine_metres, _add_walk_edges, _hms_to_seconds
from config import MAX_WALK_METRES


class TestHaversineMetres:
    def test_same_point_is_zero(self):
        assert _haversine_metres(43.6, -79.4, 43.6, -79.4) == pytest.approx(0.0, abs=0.01)

    def test_symmetry(self):
        d1 = _haversine_metres(43.6453, -79.3806, 43.5448, -80.2482)
        d2 = _haversine_metres(43.5448, -80.2482, 43.6453, -79.3806)
        assert d1 == pytest.approx(d2, rel=1e-6)

    def test_union_to_guelph_approx_70km(self):
        # Union Station (43.6453, -79.3806) → Guelph Central (43.5448, -80.2482)
        # Great-circle distance should be roughly 70 km
        d = _haversine_metres(43.6453, -79.3806, 43.5448, -80.2482)
        assert 65_000 < d < 80_000

    def test_short_walk_within_stop_radius(self):
        # ~0.003° latitude ≈ 333 m — should be within MAX_WALK_METRES (500)
        d = _haversine_metres(43.6453, -79.3806, 43.6483, -79.3806)
        assert 300 < d < 400

    def test_far_apart_exceeds_walk_radius(self):
        # Two stops ~5 km apart — clearly beyond MAX_WALK_METRES
        d = _haversine_metres(43.6453, -79.3806, 43.6, -79.34)
        assert d > 500

    def test_equator_meridian_crossing(self):
        # Sanity check with non-Toronto coordinates
        # (0°, 0°) → (0°, 1°) ≈ 111 km
        d = _haversine_metres(0.0, 0.0, 0.0, 1.0)
        assert 110_000 < d < 112_000


class TestHmsToSecondsBuilder:
    """Tests for the _hms_to_seconds copy in graph/builder.py."""

    def test_normal_time(self):
        assert _hms_to_seconds("08:30:00") == 8 * 3600 + 30 * 60

    def test_midnight(self):
        assert _hms_to_seconds("00:00:00") == 0

    def test_over_24h(self):
        assert _hms_to_seconds("25:10:00") == 25 * 3600 + 10 * 60

    def test_with_seconds(self):
        assert _hms_to_seconds("09:05:30") == 9 * 3600 + 5 * 60 + 30

    def test_invalid_returns_zero(self):
        assert _hms_to_seconds("bad") == 0

    def test_empty_returns_zero(self):
        assert _hms_to_seconds("") == 0


# ---------------------------------------------------------------------------
# _add_walk_edges — spatial index correctness
# ---------------------------------------------------------------------------

def _make_stop(stop_id: str, lat: float, lon: float):
    """Minimal Stop-like object accepted by _add_walk_edges."""
    from unittest.mock import MagicMock
    s = MagicMock()
    s.stop_id = stop_id
    s.stop_lat = lat
    s.stop_lon = lon
    return s


class TestAddWalkEdges:

    def test_no_stops_produces_no_edges(self):
        G = nx.MultiDiGraph()
        _add_walk_edges(G, [])
        assert G.number_of_edges() == 0

    def test_single_stop_no_self_loop(self):
        G = nx.MultiDiGraph()
        G.add_node("A")
        _add_walk_edges(G, [_make_stop("A", 43.6453, -79.3806)])
        assert G.number_of_edges() == 0

    def test_nearby_stops_get_walk_edge(self):
        # Two stops ~333 m apart — within MAX_WALK_METRES (500 m).
        G = nx.MultiDiGraph()
        stops = [
            _make_stop("A", 43.6453, -79.3806),
            _make_stop("B", 43.6483, -79.3806),  # ~333 m north
        ]
        for s in stops:
            G.add_node(s.stop_id)
        _add_walk_edges(G, stops)
        assert G.has_edge("A", "B")
        assert G.has_edge("B", "A")

    def test_distant_stops_get_no_walk_edge(self):
        # Two stops ~5 km apart — well beyond MAX_WALK_METRES.
        G = nx.MultiDiGraph()
        stops = [
            _make_stop("A", 43.6453, -79.3806),
            _make_stop("B", 43.6000, -79.3400),  # ~6 km away
        ]
        for s in stops:
            G.add_node(s.stop_id)
        _add_walk_edges(G, stops)
        assert not G.has_edge("A", "B")
        assert not G.has_edge("B", "A")

    def test_walk_edge_attributes(self):
        G = nx.MultiDiGraph()
        stops = [
            _make_stop("A", 43.6453, -79.3806),
            _make_stop("B", 43.6483, -79.3806),
        ]
        for s in stops:
            G.add_node(s.stop_id)
        _add_walk_edges(G, stops)
        edge = next(iter(G.get_edge_data("A", "B").values()))
        assert edge["kind"] == "walk"
        assert 300 < edge["distance_m"] < 400
        assert edge["walk_seconds"] > 0
        assert edge["weight"] == edge["walk_seconds"]

    def test_matches_brute_force(self):
        """Spatial index and O(n²) brute force produce identical edge sets."""
        import math

        # 20 stops scattered around Toronto — mix of nearby and distant pairs.
        import random
        random.seed(42)
        raw = [
            _make_stop(f"S{i}", 43.6 + random.uniform(-0.02, 0.02),
                       -79.4 + random.uniform(-0.02, 0.02))
            for i in range(20)
        ]

        # Spatial index result
        G_idx = nx.MultiDiGraph()
        for s in raw:
            G_idx.add_node(s.stop_id)
        _add_walk_edges(G_idx, raw)
        idx_edges = {(u, v) for u, v, _ in G_idx.edges(data=True)}

        # Brute-force reference
        from graph.builder import _haversine_metres
        from config import WALK_SPEED_KPH
        walk_speed_ms = WALK_SPEED_KPH * 1000 / 3600
        G_bf = nx.MultiDiGraph()
        for a in raw:
            for b in raw:
                if a.stop_id == b.stop_id:
                    continue
                dist = _haversine_metres(a.stop_lat, a.stop_lon, b.stop_lat, b.stop_lon)
                if dist <= MAX_WALK_METRES:
                    G_bf.add_edge(a.stop_id, b.stop_id)
        bf_edges = {(u, v) for u, v in G_bf.edges()}

        assert idx_edges == bf_edges
