"""
Generates top-N candidate routes between an origin and destination stop.

Algorithm:
  1. Use Yen's k-shortest paths on the transit graph (by scheduled travel time).
  2. Filter routes that violate hard constraints (too many transfers,
     unrealistic wait times, walking-only paths).
  3. Return routes as structured dicts ready for reliability scoring.

A "route" is a list of legs. Each leg is one edge traversal:
  {
    "kind":           "trip" | "walk",
    "from_stop_id":   str,
    "to_stop_id":     str,
    "from_stop_name": str,
    "to_stop_name":   str,
    # trip legs only:
    "trip_id":        str,
    "route_id":       str,
    "departure_time": str,   # HH:MM:SS
    "arrival_time":   str,   # HH:MM:SS
    "travel_seconds": int,
    # walk legs only:
    "distance_m":     float,
    "walk_seconds":   int,
  }
"""

import logging
from typing import Any

import networkx as nx

from config import MAX_ROUTES, MAX_TRANSFERS, MIN_TRANSFER_MINUTES
from graph.builder import get_graph

logger = logging.getLogger(__name__)

Route = list[dict[str, Any]]


def find_routes(
    origin_stop_id: str,
    destination_stop_id: str,
    max_routes: int = MAX_ROUTES,
) -> list[Route]:
    """
    Return up to max_routes candidate routes from origin to destination,
    ordered by total scheduled travel time (shortest first).

    Args:
        origin_stop_id:      GTFS stop_id of the departure stop.
        destination_stop_id: GTFS stop_id of the arrival stop.
        max_routes:          Maximum number of routes to return.

    Returns:
        List of routes. Each route is a list of leg dicts.

    Raises:
        ValueError: If origin or destination stop is not in the graph.
        nx.NetworkXNoPath: If no path exists.
    """
    G = get_graph()

    if origin_stop_id not in G:
        raise ValueError(f"Origin stop '{origin_stop_id}' not found in graph.")
    if destination_stop_id not in G:
        raise ValueError(f"Destination stop '{destination_stop_id}' not found in graph.")

    # Yen's algorithm returns simple paths; weight = travel_seconds
    raw_paths = nx.shortest_simple_paths(G, origin_stop_id, destination_stop_id, weight="weight")

    routes: list[Route] = []
    for node_path in raw_paths:
        if len(routes) >= max_routes * 3:  # over-generate then filter
            break
        legs = _path_to_legs(G, node_path)
        if legs is None:
            continue
        if not _passes_filters(legs):
            continue
        routes.append(legs)
        if len(routes) >= max_routes:
            break

    logger.info(
        "Found %d routes from %s to %s.", len(routes), origin_stop_id, destination_stop_id
    )
    return routes


def _path_to_legs(G: nx.MultiDiGraph, node_path: list[str]) -> Route | None:
    """
    Convert a list of node IDs into a list of leg dicts.
    Returns None if any edge is missing.

    MultiDiGraph.get_edge_data(u, v) returns {edge_key: attrs_dict, ...}.
    We pick the minimum-weight edge for each hop (fastest available connection).
    """
    legs: Route = []
    for i in range(len(node_path) - 1):
        u, v = node_path[i], node_path[i + 1]
        edges = G.get_edge_data(u, v)
        if not edges:
            return None
        # Pick the minimum-weight edge among all parallel edges (u → v)
        edge_data = min(edges.values(), key=lambda e: e.get("weight", float("inf")))
        leg: dict[str, Any] = {
            "kind": edge_data.get("kind"),
            "from_stop_id": u,
            "to_stop_id": v,
            "from_stop_name": G.nodes[u].get("name", u),
            "to_stop_name": G.nodes[v].get("name", v),
        }
        if edge_data["kind"] == "trip":
            leg.update({
                "trip_id": edge_data["trip_id"],
                "route_id": edge_data["route_id"],
                "service_id": edge_data["service_id"],
                "departure_time": edge_data["departure_time"],
                "arrival_time": edge_data["arrival_time"],
                "travel_seconds": edge_data["travel_seconds"],
            })
        else:
            leg.update({
                "distance_m": edge_data["distance_m"],
                "walk_seconds": edge_data["walk_seconds"],
            })
        legs.append(leg)
    return legs


def _passes_filters(legs: Route) -> bool:
    """
    Apply hard-constraint filters. Returns False if the route is invalid.

    Filters:
      - Must contain at least one "trip" leg (not walking-only).
      - Number of transfers must not exceed MAX_TRANSFERS.
      - Transfer wait time must be >= MIN_TRANSFER_MINUTES.
    """
    trip_legs = [l for l in legs if l["kind"] == "trip"]
    if not trip_legs:
        return False

    # Count transfers = number of times the trip_id changes between consecutive trip legs
    transfers = 0
    for i in range(1, len(trip_legs)):
        if trip_legs[i]["trip_id"] != trip_legs[i - 1]["trip_id"]:
            transfers += 1
    if transfers > MAX_TRANSFERS:
        return False

    # Check minimum transfer buffer between consecutive trip legs
    # TODO: implement wait-time calculation once we have a departure-time model
    # (requires resolving same-day service against a query datetime)

    return True


def total_travel_seconds(legs: Route) -> int:
    """Sum of all leg durations in seconds."""
    total = 0
    for leg in legs:
        if leg["kind"] == "trip":
            total += leg.get("travel_seconds", 0)
        else:
            total += leg.get("walk_seconds", 0)
    return total
