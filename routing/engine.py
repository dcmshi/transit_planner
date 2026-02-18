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


class _RouteQueryCache:
    """
    Per-call memo cache that eliminates redundant DB queries inside a single
    find_routes() invocation.

    trip_select: (route_id, first_stop, last_stop, service_date, not_before_sec)
                 → trip_id | None
        Many candidate paths share the same first/last stop on the same route
        at the same not_before time (especially paths from the Yen's main loop
        which all start at departure_dt). Cache hit avoids re-running the four-
        table JOIN every time.

    stop_times: trip_id → {stop_id: StopTime}
        Once a trip_id is resolved, all of its stop times are fetched once and
        cached. Subsequent calls for different stop subsets on the same trip
        (common in _fill_later_departures) filter the dict in Python rather
        than re-issuing a DB query.
    """

    __slots__ = ("trip_select", "stop_times")

    def __init__(self) -> None:
        self.trip_select: dict[tuple[str, str, str, str, int], str | None] = {}
        self.stop_times: dict[str, dict[str, StopTime]] = {}


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
    # with many walk edges.  15× gives a good balance: enough candidates to
    # find max_routes distinct results while keeping Yen's iterations (and the
    # DB queries each path triggers) well under the old 40× ceiling.
    MAX_CANDIDATES = max_routes * 15

    raw_paths = nx.shortest_simple_paths(H, origin_stop_id, destination_stop_id, weight="weight")

    # Per-call cache eliminates redundant DB queries across candidate paths.
    cache = _RouteQueryCache()

    seen_signatures: set[tuple[str, ...]] = set()
    routes: list[Route] = []
    candidate_paths: list[list[str]] = []
    for examined, node_path in enumerate(raw_paths):
        if examined >= MAX_CANDIDATES:
            break
        legs = _schedule_path(session, G, node_path, departure_dt, cache)
        if legs is None:
            continue
        if not _passes_filters(legs):
            continue
        sig = _route_signature(legs)
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        routes.append(legs)
        candidate_paths.append(node_path)
        if len(routes) >= max_routes:
            break

    # Fill remaining slots with later departures on already-found paths.
    if len(routes) < max_routes and candidate_paths:
        routes = _fill_later_departures(
            session, G, routes, candidate_paths, seen_signatures,
            departure_dt, max_routes, cache,
        )

    logger.info(
        "Found %d routes from %s to %s.", len(routes), origin_stop_id, destination_stop_id
    )
    return routes


# ---------------------------------------------------------------------------
# Timetable-aware path scheduling
# ---------------------------------------------------------------------------

def _pick_longest_route(G: nx.MultiDiGraph, node_path: list[str], start: int) -> str:
    """
    Among all trip-edge routes on the first stop pair of a segment, return the
    route_id that has edges covering the most consecutive stops in node_path.

    This breaks ties that arise when multiple routes share the same corridor
    with identical (often zero-second) edge weights.  Without look-ahead, the
    arbitrary dict ordering of min() can select a short-haul route that
    terminates before the intended transfer point, causing _find_trip_legs to
    return None for the full segment.
    """
    u, v = node_path[start], node_path[start + 1]
    edges = G.get_edge_data(u, v) or {}
    min_weight = min(
        (e.get("weight", float("inf")) for e in edges.values()),
        default=float("inf"),
    )
    candidates = {
        e["route_id"]
        for e in edges.values()
        if e.get("kind") == "trip" and e.get("weight", float("inf")) == min_weight
    }
    if len(candidates) == 1:
        return next(iter(candidates))

    best_route = next(iter(candidates))
    best_count = 0
    for route_id in candidates:
        count = 0
        for j in range(start, len(node_path) - 1):
            a, b = node_path[j], node_path[j + 1]
            ab_edges = G.get_edge_data(a, b) or {}
            if not any(
                e.get("kind") == "trip" and e.get("route_id") == route_id
                for e in ab_edges.values()
            ):
                break
            count += 1
        if count > best_count:
            best_count = count
            best_route = route_id
    return best_route


def _schedule_path(
    session: Session,
    G: nx.MultiDiGraph,
    node_path: list[str],
    departure_dt: datetime,
    cache: _RouteQueryCache | None = None,
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

        # Trip edge — choose the route_id that covers the most consecutive stops
        # in the node path (longest-run tie-breaking).  This prevents zero-weight
        # ties from selecting a short-haul route when a long-haul route on the
        # same corridor serves all remaining stops.
        route_id = _pick_longest_route(G, node_path, i)
        segment: list[str] = [u]
        j = i
        while j < len(node_path) - 1:
            a, b = node_path[j], node_path[j + 1]
            ab_edges = G.get_edge_data(a, b)
            if not ab_edges:
                break
            has_route = any(
                e.get("kind") == "trip" and e.get("route_id") == route_id
                for e in ab_edges.values()
            )
            if not has_route:
                break
            segment.append(b)
            j += 1

        trip_legs = _find_trip_legs(
            session, G, route_id, segment, not_before_sec, service_date, cache
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
    cache: _RouteQueryCache | None = None,
) -> Route | None:
    """
    Find the earliest trip on route_id / service_date that:
      - Departs from stops[0] at or after not_before_sec seconds past midnight.
      - Also calls at stops[-1] (with a later stop_sequence number).

    Then fetches actual stop times for every stop in the list from that trip
    and assembles leg dicts with real scheduled departure/arrival times.

    Returns None if no such trip exists or if the trip does not serve every
    stop in the list.

    The optional cache avoids redundant DB round-trips across multiple calls
    within a single find_routes() invocation:
      - trip_select: keyed by (route_id, first, last, date, not_before)
      - stop_times:  keyed by trip_id → full {stop_id: StopTime} dict
    """
    trip_key = (route_id, stops[0], stops[-1], service_date, not_before_sec)

    if cache is not None and trip_key in cache.trip_select:
        trip_id = cache.trip_select[trip_key]
    else:
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
        trip_id = row[0] if row else None
        if cache is not None:
            cache.trip_select[trip_key] = trip_id

    if trip_id is None:
        return None

    # Fetch all stop times for this trip once; cache by trip_id.
    if cache is not None and trip_id in cache.stop_times:
        full_stop_map = cache.stop_times[trip_id]
    else:
        stop_rows = (
            session.query(StopTime)
            .filter(StopTime.trip_id == trip_id)
            .order_by(StopTime.stop_sequence)
            .all()
        )
        full_stop_map = {st.stop_id: st for st in stop_rows}
        if cache is not None:
            cache.stop_times[trip_id] = full_stop_map

    stop_map: dict[str, StopTime] = {s: full_stop_map[s] for s in stops if s in full_stop_map}

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
      - Number of route_id changes must not exceed MAX_TRANSFERS.
      - Each transfer must have at least MIN_TRANSFER_MINUTES of wait time
        between the arriving trip's last arrival and the connecting trip's
        first departure.
    """
    trip_legs = [l for l in legs if l["kind"] == "trip"]
    if not trip_legs:
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
    """
    Wall-clock journey duration in seconds: last trip arrival minus first trip
    departure.  This includes transfer wait times, giving the true door-to-door
    experience rather than the sum of in-vehicle and walking time only.
    """
    trip_legs = [l for l in legs if l["kind"] == "trip"]
    if not trip_legs:
        return 0
    first_dep = _hms_to_seconds(trip_legs[0]["departure_time"])
    last_arr = _hms_to_seconds(trip_legs[-1]["arrival_time"])
    return max(0, last_arr - first_dep)


def count_transfers(legs: Route) -> int:
    """
    Number of route changes in a route (i.e. number of transfers).

    A transfer is counted each time the route_id changes between consecutive
    trip legs.  Walk legs are ignored (ADR-008).
    """
    trip_legs = [l for l in legs if l["kind"] == "trip"]
    transfers = 0
    for i in range(1, len(trip_legs)):
        if trip_legs[i]["route_id"] != trip_legs[i - 1]["route_id"]:
            transfers += 1
    return transfers


def total_walk_metres(legs: Route) -> float:
    """Total walking distance across all walk legs in metres."""
    return sum(l.get("distance_m", 0.0) for l in legs if l["kind"] == "walk")


def _fill_later_departures(
    session: Session,
    G: nx.MultiDiGraph,
    routes: list[Route],
    candidate_paths: list[list[str]],
    seen_signatures: set[tuple[str, ...]],
    departure_dt: datetime,
    max_routes: int,
    cache: _RouteQueryCache | None = None,
) -> list[Route]:
    """
    Fill remaining route slots with later departures on already-found paths.

    After the Yen's loop, deduplication may leave fewer than max_routes
    results.  This function iterates over the candidate paths in round-robin
    order.  For each path it advances the not_before pointer to 1 second past
    the first trip departure of the last result found for that path, then calls
    _schedule_path again to discover the next departure.  New trip signatures
    are added to results; known signatures are skipped but the pointer still
    advances so the following departure is tried in the next round.

    Terminates when the target count is reached or every path is exhausted
    (no more trips in the timetable for that date).

    Note: datetime cannot represent hours >= 24, so fill stops at 23:59:59.
    GO Transit Toronto–Guelph service ends well before midnight, so this is
    not a practical limitation.
    """
    travel_day = departure_dt.date()

    # Seed each path's not_before with 1 second past its first trip departure.
    path_not_before: list[int | None] = []
    for legs in routes:
        first_trip = next((l for l in legs if l["kind"] == "trip"), None)
        if first_trip:
            path_not_before.append(_hms_to_seconds(first_trip["departure_time"]) + 1)
        else:
            path_not_before.append(None)

    MAX_SECONDS = 23 * 3600 + 59 * 60 + 59  # 23:59:59

    while len(routes) < max_routes:
        any_active = False
        for i, node_path in enumerate(candidate_paths):
            if len(routes) >= max_routes:
                break
            nb = path_not_before[i]
            if nb is None or nb > MAX_SECONDS:
                continue
            any_active = True
            h, m, s = nb // 3600, (nb % 3600) // 60, nb % 60
            next_dt = datetime(travel_day.year, travel_day.month, travel_day.day, h, m, s)
            legs = _schedule_path(session, G, node_path, next_dt, cache)
            if legs is None or not _passes_filters(legs):
                path_not_before[i] = None
                continue
            # Always advance the pointer so the next round tries the departure after this one.
            first_trip = next((l for l in legs if l["kind"] == "trip"), None)
            path_not_before[i] = (
                _hms_to_seconds(first_trip["departure_time"]) + 1
            ) if first_trip else None
            sig = _route_signature(legs)
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                routes.append(legs)
        if not any_active:
            break

    return routes


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
