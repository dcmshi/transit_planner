from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# GET /stops
# ---------------------------------------------------------------------------

class StopResult(BaseModel):
    stop_id: str
    stop_name: str
    lat: float
    lon: float
    routes_served: list[str]


# ---------------------------------------------------------------------------
# GET /routes — building blocks
# ---------------------------------------------------------------------------

class LiveRisk(BaseModel):
    risk_score: float
    risk_label: Literal["Low", "Medium", "High"]
    modifiers: list[str]
    is_cancelled: bool
    # Which reliability bucket produced risk_score.  Matches the time_bucket
    # of the /reliability row for this leg's (route_id, from_stop_id), so a
    # client can show the counters behind the score without re-deriving the
    # bucketing rules.  Populated even when the bucket had too little data and
    # the neutral prior was used.
    time_bucket: str
    # The stored counters behind risk_score — the same numbers /reliability
    # returns for this (route_id, from_stop_id, time_bucket) — so a client can
    # explain a leg's score without a second request.  All zero, with source
    # null, when no record exists for the bucket at all.
    scheduled_departures: float
    observed_departures: float
    total_delay_seconds: float
    cancellation_count: float
    source: str | None
    # True when the record was too sparse to score, or absent, and the neutral
    # prior stood in — so the UI can say "no observations yet for this bucket".
    neutral_prior_used: bool


class TripLeg(BaseModel):
    kind: Literal["trip"]
    from_stop_id: str
    to_stop_id: str
    from_stop_name: str
    to_stop_name: str
    # Stop coordinates, so a client can draw the leg without a lookup per
    # stop.  Null only if the graph node lacks them, which ingested stops
    # never do.
    from_lat: float | None = None
    from_lon: float | None = None
    to_lat: float | None = None
    to_lon: float | None = None
    trip_id: str
    route_id: str
    service_id: str
    departure_time: str   # HH:MM:SS — may exceed 24:00:00
    arrival_time: str     # HH:MM:SS — may exceed 24:00:00
    travel_seconds: int
    # Track geometry between this leg's two stops as a Google encoded
    # polyline (precision 5), simplified for display.  Decode with any
    # standard decoder — @mapbox/polyline's toGeoJSON() yields [lon, lat]
    # pairs ready for MapLibre.  Null when the trip has no usable GTFS
    # shape; clients should then fall back to a straight line between the
    # stop coordinates.  Walk legs never have one: GTFS publishes no
    # pedestrian geometry.
    geometry: str | None = None
    risk: LiveRisk | None
    # Live GTFS-RT delay — present only for same-day trips currently in the
    # trip-updates feed with a non-zero delay (positive = late).
    live_delay_seconds: int | None = None
    expected_departure: str | None = None  # scheduled + live delay, HH:MM:SS
    expected_arrival: str | None = None


class WalkLeg(BaseModel):
    kind: Literal["walk"]
    from_stop_id: str
    to_stop_id: str
    from_stop_name: str
    to_stop_name: str
    # Stop coordinates, so a client can draw the leg without a lookup per
    # stop.  Null only if the graph node lacks them, which ingested stops
    # never do.
    from_lat: float | None = None
    from_lon: float | None = None
    to_lat: float | None = None
    to_lon: float | None = None
    distance_m: float
    walk_seconds: int


Leg = Annotated[TripLeg | WalkLeg, Field(discriminator="kind")]


class ScoredRoute(BaseModel):
    legs: list[Leg]
    total_travel_seconds: int
    transfers: int
    total_walk_metres: float
    risk_score: float
    risk_label: Literal["Low", "Medium", "High"]


class RoutesResponse(BaseModel):
    routes: list[ScoredRoute]
    explanation: str | None = None


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class GtfsStats(BaseModel):
    stops: int
    trips: int
    latest_service_date: str | None
    graph_nodes: int
    graph_edges: int
    graph_built: bool
    last_built_at: str | None
    next_refresh_at: str | None


class ReliabilityStats(BaseModel):
    records: int
    last_seeded_at: str | None
    # Record counts by provenance ("seed" / "mixed" / "observed") — how much
    # of the risk model is synthetic priors vs real GTFS-RT observations.
    by_source: dict[str, int]


class GtfsRtStats(BaseModel):
    polling_active: bool
    startup_fetch_only: bool
    # Feed health — how fresh the RT data actually is.
    last_fetched_at: str | None
    consecutive_failures: int
    backing_off_until: str | None
    polling_coverage_since: str | None  # start of continuous coverage (no-show sweeps)
    trip_updates: int
    service_alerts: int
    vehicle_positions: int


class HealthResponse(BaseModel):
    status: Literal["ok"]
    timestamp: str
    gtfs: GtfsStats
    reliability: ReliabilityStats
    gtfs_rt: GtfsRtStats


# ---------------------------------------------------------------------------
# GET /reliability
# ---------------------------------------------------------------------------

class ReliabilityResult(BaseModel):
    route_id: str | None
    stop_id: str | None
    time_bucket: str | None
    # "seed" (synthetic prior), "observed" (real RT only), or "mixed".
    source: str
    scheduled_departures: float
    observed_departures: float
    total_delay_seconds: float
    cancellation_count: float
    window_start_date: str | None
    window_end_date: str | None
    updated_at: str | None
    # None when the record holds too little data to score, in which case the
    # scorer substitutes the neutral prior instead of this record.
    score: float | None
    neutral_prior_used: bool


# ---------------------------------------------------------------------------
# GET /alerts
# ---------------------------------------------------------------------------

class AlertResult(BaseModel):
    alert_id: str
    header: str
    description: str
    affected_route_ids: list[str]
    affected_stop_ids: list[str]
    fetched_at: str


# ---------------------------------------------------------------------------
# POST /ingest/*
# ---------------------------------------------------------------------------

class IngestResponse(BaseModel):
    status: Literal["accepted"]
    message: str


class IngestStatusResponse(BaseModel):
    running: bool
    started_at: str | None
    finished_at: str | None
    last_status: Literal["ok", "error"] | None
    last_message: str | None


class SeedResponse(BaseModel):
    status: Literal["ok"]
    records_written: int
    message: str
