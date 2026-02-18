"""
Integration tests for PostGIS ST_DWithin walk edges.

Skipped unless DATABASE_URL points to a real PostgreSQL + PostGIS instance.
Run with:
    DATABASE_URL=postgresql+psycopg://transit:transit@localhost:5432/transit \
    uv run pytest tests/integration/ -q
"""

import os

import networkx as nx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in DATABASE_URL,
    reason="requires PostgreSQL + PostGIS (set DATABASE_URL env var)",
)


@pytest.fixture(scope="module")
def pg_session():
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _insert_stop(session, stop_id, lat, lon):
    """Insert a minimal stop row with geog populated."""
    session.execute(text("""
        INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon, stop_code, geog)
        VALUES (:id, :name, :lat, :lon, '', ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography)
        ON CONFLICT (stop_id) DO NOTHING
    """), {"id": stop_id, "name": stop_id, "lat": lat, "lon": lon})
    session.commit()


def _delete_stops(session, stop_ids):
    for sid in stop_ids:
        session.execute(text("DELETE FROM stops WHERE stop_id = :id"), {"id": sid})
    session.commit()


class TestWalkEdgesPostGIS:

    def test_nearby_stops_linked(self, pg_session):
        """Two stops ~333 m apart get a walk edge via ST_DWithin."""
        from graph.builder import _add_walk_edges_postgis
        from config import MAX_WALK_METRES

        # Stops ~333 m apart (0.003° lat ≈ 333 m)
        _insert_stop(pg_session, "_TEST_A", 43.6453, -79.3806)
        _insert_stop(pg_session, "_TEST_B", 43.6483, -79.3806)
        try:
            G = nx.MultiDiGraph()
            G.add_node("_TEST_A")
            G.add_node("_TEST_B")
            _add_walk_edges_postgis(G, pg_session)
            assert G.has_edge("_TEST_A", "_TEST_B"), "nearby stops should be linked"
            assert G.has_edge("_TEST_B", "_TEST_A"), "walk edges are bidirectional"
            edge = next(iter(G.get_edge_data("_TEST_A", "_TEST_B").values()))
            assert edge["kind"] == "walk"
            assert 300 < edge["distance_m"] < 400
            assert edge["walk_seconds"] > 0
        finally:
            _delete_stops(pg_session, ["_TEST_A", "_TEST_B"])

    def test_distant_stops_not_linked(self, pg_session):
        """Two stops ~6 km apart do not get a walk edge."""
        from graph.builder import _add_walk_edges_postgis

        _insert_stop(pg_session, "_TEST_C", 43.6453, -79.3806)
        _insert_stop(pg_session, "_TEST_D", 43.6000, -79.3400)
        try:
            G = nx.MultiDiGraph()
            G.add_node("_TEST_C")
            G.add_node("_TEST_D")
            _add_walk_edges_postgis(G, pg_session)
            assert not G.has_edge("_TEST_C", "_TEST_D")
            assert not G.has_edge("_TEST_D", "_TEST_C")
        finally:
            _delete_stops(pg_session, ["_TEST_C", "_TEST_D"])

    def test_matches_bisect_result(self, pg_session):
        """PostGIS and bisect produce the same edge set for a small stop cluster."""
        from graph.builder import _add_walk_edges_postgis, _add_walk_edges_bisect
        from unittest.mock import MagicMock

        stops_data = [
            ("_TEST_E", 43.6453, -79.3806),
            ("_TEST_F", 43.6458, -79.3810),  # ~60 m
            ("_TEST_G", 43.6500, -79.3806),  # ~520 m — just outside 500 m limit
        ]
        for sid, lat, lon in stops_data:
            _insert_stop(pg_session, sid, lat, lon)

        try:
            # PostGIS result
            G_pg = nx.MultiDiGraph()
            for sid, *_ in stops_data:
                G_pg.add_node(sid)
            _add_walk_edges_postgis(G_pg, pg_session)
            pg_edges = {(u, v) for u, v, _ in G_pg.edges(data=True)}

            # Bisect result (in-process, no DB)
            mock_stops = []
            for sid, lat, lon in stops_data:
                s = MagicMock()
                s.stop_id = sid
                s.stop_lat = lat
                s.stop_lon = lon
                mock_stops.append(s)
            G_bx = nx.MultiDiGraph()
            for sid, *_ in stops_data:
                G_bx.add_node(sid)
            _add_walk_edges_bisect(G_bx, mock_stops)
            bx_edges = {(u, v) for u, v, _ in G_bx.edges(data=True)}

            assert pg_edges == bx_edges, (
                f"PostGIS and bisect disagree.\n  PostGIS: {pg_edges}\n  Bisect: {bx_edges}"
            )
        finally:
            _delete_stops(pg_session, [sid for sid, *_ in stops_data])
