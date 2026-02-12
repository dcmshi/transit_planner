"""
Generates top-N candidate routes between an origin and destination stop.

Algorithm:
  1. Use Yen's k-shortest paths on the transit graph (by scheduled travel time)
     to discover promising stop sequences.
  2. For each candidate sequence, query the database to find actual trips that
     run on the requested date and depart at or after the requested time
     (_schedule_path).  This replaces the previous time-agnostic leg assembly
     and ensures every route has coherent, real departure/arrival times.
  3. Filter routes that violate hard constraints (zero-second legs, too many
     transfers, tight connections).
  4. Return routes as structured dicts ready for reliability scoring.

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
    "service_id":     str,   # YYYYMMDD — the date this trip runs
    "departure_time": str,   # HH:MM:SS (may exceed 24:00:00)
    "arrival_time":   str,   # HH:MM:SS
    "travel_seconds": int,
    # walk legs only:
    "distance_m":     float,
    "walk_seconds":   int,
  }
"""

import logging
from datetime import datetime
from typing import Any

import networkx as nx
from sqlalchemy import text
from sqlalchemy.orm import Session

from config import MAX_ROUTES, MAX_TRANSFERS, MIN_TRANSFER_MINUTES
from db.models import StopTime
from graph.builder import get_graph

logger = logging.getLogger(__name__)

Route = list[dict[str, Any]]


def find_routes(
    origin_stop_id: str,
    destination_stop_id: str,
    departure_dt: datetime,
    session: Session,
    max_routes: int = MAX_ROUTES,
) -> list[Route]:
    """
    Return up to max_routes candidate routes from origin to destination,
    with coherent real-world departure/arrival times for the requested date
    and time.

    Args:
        origin_stop_id:      GTFS stop_id of the departure stop.
        destination_stop_id: GTFS stop_id of the arrival stop.
        departure_dt:        Earliest acceptable departure date and time.
        session:             SQLAlchemy session for timetable queries.
        max_routes:          Maximum number of routes to return.

    Returns:
        List of routes, each a list of leg dicts with actual scheduled times.

    Raises:
        ValueError: If origin or destination stop is not in the graph.
        nx.NetworkXNoPath: If no path exists between the stops.
    """
    G = get_graph()

    if origin_stop_id not in G:
        raise ValueError(f"Origin stop '{origin_stop_id}' not found in graph.")
    if destination_stop_id not in G:
        raise ValueError(f"Destination stop '{destination_stop_id}' not found in graph.")

    # shortest_simple_paths (Yen's algorithm) does not support MultiDiGraph, so
    # first project to a DiGraph keeping only the min-weight edge per (u, v) pair.
    H = nx.DiGraph()
    H.add_nodes_from(G.nodes(data=True))
    for u, v, edge_data in G.edges(data=True):
        w = edge_data.get("weight", float("inf"))
        if not H.has_edge(u, v) or H[u][v]["weight"] > w:
            H.add_edge(u, v, weight=w)

    # Hard cap on total candidate paths examined — prevents hanging on graphs
    # with many walk edges.  Raised vs the old value because cheap dedup now
    # discards same-trip duplicates quickly, so we can afford to look further.
    MAX_CANDIDATES = max_routes * 40

    raw_paths = nx.shortest_simple_paths(H, origin_stop_id, destination_stop_id, weight="weight")

    seen_signatures: set[tuple[str, ...]] = set()
    routes: list[Route] = []
    for examined, node_path in enumerate(raw_paths):
        if examined >= MAX_CANDIDATES:
            break
        legs = _schedule_path(session, G, node_path, departure_dt)
        if legs is None:
            continue
        if not _passes_filters(legs):
            continue
        sig = _route_signature(legs)
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        routes.append(legs)
        if len(routes) >= max_routes:
            break

    logger.info(
        "Found %d routes from %s to %s.", len(routes), origin_stop_id, destination_stop_id
    )
    return routes


# ---------------------------------------------------------------------------
# Timetable-aware path scheduling
# ---------------------------------------------------------------------------

def _schedule_path(
    session: Session,
    G: nx.MultiDiGraph,
    node_path: list[str],
    departure_dt: datetime,
) -> Route | None:
    """
    Convert a graph node-path into time-coherent legs.

    Groups consecutive trip-edge stops by route_id into segments.  For each
    segment, queries the database for the earliest real trip running on
    departure_dt's date that departs from the segment's first stop at or after
    the running not_before time.  Walk legs are threaded in between.

    Returns None if any trip segment has no viable trip (no service on that
    date, or last departure already passed).
    """
    service_date = departure_dt.strftime("%Y%m%d")
    not_before_sec = (
        departure_dt.hour * 3600 + departure_dt.minute * 60 + departure_dt.second
    )

    legs: Route = []
    i = 0
    while i < len(node_path) - 1:
        u, v = node_path[i], node_path[i + 1]
        edges = G.get_edge_data(u, v)
        if not edges:
            return None
        best = min(edges.values(), key=lambda e: e.get("weight", float("inf")))

        if best["kind"] == "walk":
            legs.append({
                "kind": "walk",
                "from_stop_id": u,
                "to_stop_id": v,
                "from_stop_name": G.nodes[u].get("name", u),
                "to_stop_name": G.nodes[v].get("name", v),
                "distance_m": best["distance_m"],
                "walk_seconds": best["walk_seconds"],
            })
            not_before_sec += best["walk_seconds"]
            i += 1
            continue

        # Trip edge — extend the segment as far as the same route_id continues.
        route_id = best["route_id"]
        segment: list[str] = [u]
        j = i
        while j < len(node_path) - 1:
            a, b = node_path[j], node_path[j + 1]
            ab_edges = G.get_edge_data(a, b)
            if not ab_edges:
                break
            best_ab = min(ab_edges.values(), key=lambda e: e.get("weight", float("inf")))
            if best_ab["kind"] != "trip" or best_ab["route_id"] != route_id:
                break
            segment.append(b)
            j += 1

        trip_legs = _find_trip_legs(
            session, G, route_id, segment, not_before_sec, service_date
        )
        if trip_legs is None:
            return None

        legs.extend(trip_legs)
        # Advance the clock to arrival at the end of this segment; add
        # the minimum transfer buffer before the next segment can depart.
        last_arr_sec = _hms_to_seconds(trip_legs[-1]["arrival_time"])
        not_before_sec = last_arr_sec + MIN_TRANSFER_MINUTES * 60
        i = j

    return legs if legs else None


def _find_trip_legs(
    session: Session,
    G: nx.MultiDiGraph,
    route_id: str,
    stops: list[str],
    not_before_sec: int,
    service_date: str,
) -> Route | None:
    """
    Find the earliest trip on route_id / service_date that:
      - Departs from stops[0] at or after not_before_sec seconds past midnight.
      - Also calls at stops[-1] (with a later stop_sequence number).

    Then fetches actual stop times for every stop in the list from that trip
    and assembles leg dicts with real scheduled departure/arrival times.

    Returns None if no such trip exists or if the trip does not serve every
    stop in the list.
    """
    row = session.execute(
        text("""
            SELECT st_first.trip_id
            FROM stop_times st_first
            JOIN trips t ON t.trip_id = st_first.trip_id
            JOIN stop_times st_last
              ON st_last.trip_id  = st_first.trip_id
             AND st_last.stop_id  = :last_stop
             AND st_last.stop_sequence > st_first.stop_sequence
            WHERE st_first.stop_id  = :first_stop
              AND t.route_id        = :route_id
              AND t.service_id      = :service_date
              AND (
                    CAST(substr(st_first.departure_time, 1, 2) AS INT) * 3600
                  + CAST(substr(st_first.departure_time, 4, 2) AS INT) * 60
                  + CAST(substr(st_first.departure_time, 7, 2) AS INT)
                ) >= :not_before
            ORDER BY st_first.departure_time ASC
            LIMIT 1
        """),
        {
            "first_stop":   stops[0],
            "last_stop":    stops[-1],
            "route_id":     route_id,
            "service_date": service_date,
            "not_before":   not_before_sec,
        },
    ).fetchone()

    if row is None:
        return None

    trip_id = row[0]

    # Fetch actual stop times for every stop in the segment from this trip.
    stop_rows = (
        session.query(StopTime)
        .filter(StopTime.trip_id == trip_id, StopTime.stop_id.in_(stops))
        .order_by(StopTime.stop_sequence)
        .all()
    )
    stop_map: dict[str, StopTime] = {st.stop_id: st for st in stop_rows}

    # Verify every stop in our segment is covered by this trip.
    if any(stop not in stop_map for stop in stops):
        return None

    legs: Route = []
    for k in range(len(stops) - 1):
        a, b = stops[k], stops[k + 1]
        dep = stop_map[a].departure_time
        arr = stop_map[b].arrival_time
        dep_sec = _hms_to_seconds(dep)
        arr_sec = _hms_to_seconds(arr)
        legs.append({
            "kind":           "trip",
            "from_stop_id":   a,
            "to_stop_id":     b,
            "from_stop_name": G.nodes[a].get("name", a),
            "to_stop_name":   G.nodes[b].get("name", b),
            "trip_id":        trip_id,
            "route_id":       route_id,
            "service_id":     service_date,
            "departure_time": dep,
            "arrival_time":   arr,
            "travel_seconds": max(0, arr_sec - dep_sec),
        })

    return legs


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _passes_filters(legs: Route) -> bool:
    """
    Apply hard-constraint filters. Returns False if the route is invalid.

    Filters:
      - Must contain at least one "trip" leg (not walking-only).
      - No trip leg may have travel_seconds == 0.  GO GTFS uses 1-minute
        resolution; zero-second legs are street-stop artifacts that appear on
        local bus routes where consecutive stops share the same scheduled
        minute.
      - Number of route_id changes must not exceed MAX_TRANSFERS.
      - Each transfer must have at least MIN_TRANSFER_MINUTES of wait time
        between the arriving trip's last arrival and the connecting trip's
        first departure.
    """
    trip_legs = [l for l in legs if l["kind"] == "trip"]
    if not trip_legs:
        return False

    # Reject any route that contains a zero-second trip leg.
    if any(l.get("travel_seconds", 0) == 0 for l in trip_legs):
        return False

    # Count transfers = number of times the route_id changes between consecutive
    # trip legs.  Adjacent edges on the same route may have different trip_ids
    # (the graph picks minimum-travel-time edges independently), so route_id is
    # the right signal here (ADR-008).
    transfers = 0
    for i in range(1, len(trip_legs)):
        if trip_legs[i]["route_id"] != trip_legs[i - 1]["route_id"]:
            transfers += 1
    if transfers > MAX_TRANSFERS:
        return False

    # Minimum transfer buffer: at each route change, the connecting departure
    # must be at least MIN_TRANSFER_MINUTES after the arriving trip's arrival.
    min_buffer_sec = MIN_TRANSFER_MINUTES * 60
    for i in range(1, len(trip_legs)):
        if trip_legs[i]["route_id"] != trip_legs[i - 1]["route_id"]:
            arr_sec = _hms_to_seconds(trip_legs[i - 1]["arrival_time"])
            dep_sec = _hms_to_seconds(trip_legs[i]["departure_time"])
            if dep_sec - arr_sec < min_buffer_sec:
                return False

    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _route_signature(legs: Route) -> tuple[str, ...]:
    """
    Produce a deduplication key for a scheduled route.

    Two routes are considered the same physical journey if they ride
    exactly the same trips in the same order.  Walk legs are ignored;
    consecutive legs sharing a trip_id are collapsed to one entry.

    Example:
        Trip T1 (A→B), walk (B→B'), Trip T2 (B'→C)  →  ("T1", "T2")
        Trip T1 (A→C, skipping B)                    →  ("T1",)   ← duplicate of above if T1==T1
    """
    sig: list[str] = []
    for leg in legs:
        if leg["kind"] == "trip":
            trip_id = leg["trip_id"]
            if not sig or sig[-1] != trip_id:
                sig.append(trip_id)
    return tuple(sig)


def total_travel_seconds(legs: Route) -> int:
    """Sum of all leg durations in seconds (trip travel + walk time)."""
    total = 0
    for leg in legs:
        if leg["kind"] == "trip":
            total += leg.get("travel_seconds", 0)
        else:
            total += leg.get("walk_seconds", 0)
    return total


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
