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


class TripLeg(BaseModel):
    kind: Literal["trip"]
    from_stop_id: str
    to_stop_id: str
    from_stop_name: str
    to_stop_name: str
    trip_id: str
    route_id: str
    service_id: str
    departure_time: str   # HH:MM:SS — may exceed 24:00:00
    arrival_time: str     # HH:MM:SS — may exceed 24:00:00
    travel_seconds: int
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
    status: Literal["ok"]
    message: str


class SeedResponse(BaseModel):
    status: Literal["ok"]
    records_written: int
    message: str
