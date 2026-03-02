"""
Unit tests for graph.builder pure functions.

_haversine_metres and _hms_to_seconds are private helpers but are
tested directly since they encapsulate meaningful logic.
"""

import pytest
import networkx as nx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, Route, Stop, StopTime, Trip
from graph.builder import (
    _haversine_metres,
    _add_walk_edges_bisect,
    _hms_to_seconds,
    build_graph,
    get_graph,
    get_projected_graph,
)
import graph.builder as builder_mod
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
# _add_walk_edges_bisect — spatial index correctness
# ---------------------------------------------------------------------------

def _make_stop(stop_id: str, lat: float, lon: float):
    """Minimal Stop-like object accepted by _add_walk_edges_bisect."""
    from unittest.mock import MagicMock
    s = MagicMock()
    s.stop_id = stop_id
    s.stop_lat = lat
    s.stop_lon = lon
    return s


class TestAddWalkEdges:

    def test_no_stops_produces_no_edges(self):
        G = nx.MultiDiGraph()
        _add_walk_edges_bisect(G, [])
        assert G.number_of_edges() == 0

    def test_single_stop_no_self_loop(self):
        G = nx.MultiDiGraph()
        G.add_node("A")
        _add_walk_edges_bisect(G, [_make_stop("A", 43.6453, -79.3806)])
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
        _add_walk_edges_bisect(G, stops)
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
        _add_walk_edges_bisect(G, stops)
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
        _add_walk_edges_bisect(G, stops)
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
        _add_walk_edges_bisect(G_idx, raw)
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


# ---------------------------------------------------------------------------
# build_graph / get_graph / get_projected_graph
# ---------------------------------------------------------------------------

@pytest.fixture
def graph_db():
    """In-memory SQLite with a minimal two-stop, one-trip GTFS dataset."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    session.add(Stop(stop_id="S1", stop_name="Stop One", stop_lat=43.6453, stop_lon=-79.3806))
    session.add(Stop(stop_id="S2", stop_name="Stop Two", stop_lat=43.6483, stop_lon=-79.3806))
    session.add(Route(route_id="R1", route_short_name="1", route_long_name="Test", route_type=3))
    session.add(Trip(trip_id="T1", route_id="R1", service_id="20260209",
                     trip_headsign="Test", direction_id=0))
    session.add(StopTime(trip_id="T1", stop_id="S1", stop_sequence=1,
                         departure_time="08:00:00", arrival_time="08:00:00"))
    session.add(StopTime(trip_id="T1", stop_id="S2", stop_sequence=2,
                         departure_time="08:30:00", arrival_time="08:30:00"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


class TestGetGraphBeforeBuild:

    def test_get_graph_raises_before_build(self):
        builder_mod._graph = None
        with pytest.raises(RuntimeError, match="not been built"):
            get_graph()

    def test_get_projected_graph_raises_before_build(self):
        builder_mod._digraph = None
        with pytest.raises(RuntimeError, match="not been built"):
            get_projected_graph()


class TestBuildGraph:

    def test_nodes_created_for_all_stops(self, graph_db):
        G = build_graph(graph_db)
        assert "S1" in G.nodes
        assert "S2" in G.nodes

    def test_node_has_name_attribute(self, graph_db):
        G = build_graph(graph_db)
        assert G.nodes["S1"]["name"] == "Stop One"
        assert G.nodes["S2"]["name"] == "Stop Two"

    def test_trip_edge_created(self, graph_db):
        G = build_graph(graph_db)
        assert G.has_edge("S1", "S2")
        edge = next(iter(G.get_edge_data("S1", "S2").values()))
        assert edge["kind"] == "trip"
        assert edge["route_id"] == "R1"

    def test_walk_edges_created_for_nearby_stops(self, graph_db):
        # S1 and S2 are ~333 m apart — within MAX_WALK_METRES (500 m)
        G = build_graph(graph_db)
        walk_edges = [
            d for _, _, d in G.edges(data=True)
            if d.get("kind") == "walk"
        ]
        assert len(walk_edges) > 0

    def test_projected_digraph_cached(self, graph_db):
        build_graph(graph_db)
        H = get_projected_graph()
        assert H is not None
        assert H.has_edge("S1", "S2")

    def test_trip_edge_deduplication_keeps_min_travel_time(self, graph_db):
        """Two trips on same route between same stops: only min travel time kept."""
        # Add a slower second trip on the same route
        graph_db.add(Trip(trip_id="T2", route_id="R1", service_id="20260209",
                          trip_headsign="Test", direction_id=0))
        graph_db.add(StopTime(trip_id="T2", stop_id="S1", stop_sequence=1,
                              departure_time="09:00:00", arrival_time="09:00:00"))
        graph_db.add(StopTime(trip_id="T2", stop_id="S2", stop_sequence=2,
                              departure_time="10:00:00", arrival_time="10:00:00"))  # 60-min trip
        graph_db.commit()

        G = build_graph(graph_db)
        trip_edges = [
            d for _, _, d in G.edges("S1", data=True)
            if d.get("kind") == "trip" and d.get("route_id") == "R1"
        ]
        # Only one edge per (from_stop, to_stop, route_id) — the 30-min one
        assert len(trip_edges) == 1
        assert trip_edges[0]["travel_seconds"] == 30 * 60
