"""
Builds a directed graph of transit stops and edges from GTFS data.

Graph structure:
  Nodes  — stop_id strings, attributed with {name, lat, lon}
  Edges  — two kinds:
    "trip"    : stop A → stop B on a scheduled bus trip
                attrs: {trip_id, route_id, departure_time, arrival_time,
                        travel_seconds, service_id, kind="trip"}
    "walk"    : stop A → stop B within MAX_WALK_METRES
                attrs: {distance_m, walk_seconds, kind="walk"}

The graph is built once from the DB after static ingestion and cached
in memory. It must be rebuilt after each daily GTFS refresh.
"""

import logging
import math
from typing import Optional

import networkx as nx
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from config import MAX_WALK_METRES, WALK_SPEED_KPH
from db.models import Stop, StopTime, Trip

logger = logging.getLogger(__name__)

# Module-level cached graph
_graph: Optional[nx.MultiDiGraph] = None


def get_graph() -> nx.MultiDiGraph:
    """Return the cached transit graph. Raises if not yet built."""
    if _graph is None:
        raise RuntimeError("Transit graph has not been built yet. Call build_graph() first.")
    return _graph


def build_graph(session: Session) -> nx.MultiDiGraph:
    """
    Construct and cache the full transit + walking graph from the database.
    Returns the graph and stores it in the module-level cache.
    """
    global _graph
    G = nx.MultiDiGraph()

    stops = session.query(Stop).all()
    _add_stop_nodes(G, stops)
    _add_trip_edges(G, session)
    _add_walk_edges(G, stops)

    _graph = G
    logger.info(
        "Graph built: %d nodes, %d edges (%d trip, %d walk).",
        G.number_of_nodes(),
        G.number_of_edges(),
        sum(1 for *_, d in G.edges(data=True) if d.get("kind") == "trip"),
        sum(1 for *_, d in G.edges(data=True) if d.get("kind") == "walk"),
    )
    return G


def _add_stop_nodes(G: nx.MultiDiGraph, stops: list[Stop]) -> None:
    for stop in stops:
        G.add_node(stop.stop_id, name=stop.stop_name, lat=stop.stop_lat, lon=stop.stop_lon)


def _add_trip_edges(G: nx.MultiDiGraph, session: Session) -> None:
    """
    For every consecutive stop pair on every trip, add a directed edge.

    Uses a single SQL join query (rather than ORM relationship loading) to
    handle the large GO Transit dataset (~125K trips, ~2M stop times) without
    loading full ORM objects into memory.

    Deduplicates by (from_stop, to_stop, route_id), keeping only the minimum
    travel-time edge per route per stop pair. This gives the graph one edge per
    route for each physical connection — enough for reliable pathfinding and
    reliability scoring — without bloating to 2M+ edges.
    """
    rows = session.execute(
        sa_select(
            StopTime.trip_id,
            StopTime.stop_id,
            StopTime.departure_time,
            StopTime.arrival_time,
            StopTime.stop_sequence,
            Trip.route_id,
            Trip.service_id,
        )
        .join(Trip, StopTime.trip_id == Trip.trip_id)
        .order_by(StopTime.trip_id, StopTime.stop_sequence)
    ).all()

    # Best edge per (from_stop, to_stop, route_id) — minimum travel time
    best: dict[tuple[str, str, str], dict] = {}

    current_trip_id: str | None = None
    current_trip: list = []

    def _flush(trip_rows: list) -> None:
        for i in range(len(trip_rows) - 1):
            a, b = trip_rows[i], trip_rows[i + 1]
            dep_sec = _hms_to_seconds(a.departure_time)
            arr_sec = _hms_to_seconds(b.arrival_time)
            travel_sec = max(0, arr_sec - dep_sec)
            key = (a.stop_id, b.stop_id, a.route_id)
            if key not in best or travel_sec < best[key]["travel_seconds"]:
                best[key] = {
                    "trip_id": a.trip_id,
                    "route_id": a.route_id,
                    "service_id": a.service_id,
                    "departure_time": a.departure_time,
                    "arrival_time": b.arrival_time,
                    "travel_seconds": travel_sec,
                    "weight": travel_sec,
                    "kind": "trip",
                }

    for row in rows:
        if row.trip_id != current_trip_id:
            if current_trip:
                _flush(current_trip)
            current_trip_id = row.trip_id
            current_trip = [row]
        else:
            current_trip.append(row)
    if current_trip:
        _flush(current_trip)

    for (from_stop, to_stop, _), attrs in best.items():
        G.add_edge(from_stop, to_stop, **attrs)

    logger.info("Added %d trip edges (%d unique route/stop-pair combinations).", len(best), len(best))


def _add_walk_edges(G: nx.MultiDiGraph, stops: list[Stop]) -> None:
    """
    Add bidirectional walking edges between stops within MAX_WALK_METRES.
    Walk time (seconds) = distance / walk speed.
    """
    walk_speed_ms = WALK_SPEED_KPH * 1000 / 3600  # metres per second

    for i, a in enumerate(stops):
        for j, b in enumerate(stops):
            if i == j:
                continue
            dist = _haversine_metres(a.stop_lat, a.stop_lon, b.stop_lat, b.stop_lon)
            if dist <= MAX_WALK_METRES:
                walk_sec = int(dist / walk_speed_ms)
                G.add_edge(
                    a.stop_id,
                    b.stop_id,
                    distance_m=dist,
                    walk_seconds=walk_sec,
                    weight=walk_sec,
                    kind="walk",
                )


def _haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two (lat, lon) points in metres."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _hms_to_seconds(hms: str) -> int:
    """
    Convert HH:MM:SS (possibly HH > 23) to integer seconds past midnight.
    Returns 0 on parse failure.
    """
    try:
        parts = hms.strip().split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        return 0
