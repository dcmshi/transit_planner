# GO Transit Reliability Router — Progress Tracker

## Architecture Overview

```
GTFS Static (daily)  ──► ingestion/gtfs_static.py ──► SQLite DB
                                                           │
GTFS-RT (30–60s)     ──► ingestion/gtfs_realtime.py        │
                              │ (in-memory state)           │
                              │                             ▼
                              │                    graph/builder.py
                              │                    (networkx MultiDiGraph)
                              │                             │
                              │                             ▼
                              │                    routing/engine.py
                              │                    (Yen's k-shortest paths)
                              │                             │
                              └────────────────────►        ▼
                                                   reliability/
                                                   historical.py + live.py
                                                   P(miss) = f(hist, live)
                                                             │
                                                             ▼
                                                   llm/explainer.py
                                                   (plain-language only)
                                                             │
                                                             ▼
                                                   api/main.py (FastAPI)
                                                   GET /routes
```

---

## Module Status

| Module                          | Status      | Notes                                                              |
|---------------------------------|-------------|--------------------------------------------------------------------|
| `db/models.py`                  | Complete    | SQLAlchemy models for GTFS + reliability                           |
| `db/session.py`                 | Complete    | Engine, SessionLocal, get_session                                  |
| `ingestion/gtfs_static.py`      | Complete    | Download, parse, store all GTFS CSVs                               |
| `ingestion/gtfs_realtime.py`    | Complete    | Live — `poll_all()` feeds three RT feeds; `observe_departures(session)` accumulates real cancellations/delays into `ReliabilityRecord`; `_recorded_today` deduplicates across 30-second cycles |
| `graph/builder.py`              | Complete    | MultiDiGraph; one edge per (stop-pair, route_id); single SQL join  |
| `routing/engine.py`             | Complete    | Yen's k-shortest paths on projected DiGraph; MAX_CANDIDATES cap    |
| `reliability/historical.py`     | Complete    | Rolling-window score per route/stop/bucket                         |
| `reliability/live.py`           | Complete    | Live GTFS-RT risk modifiers                                        |
| `llm/explainer.py`              | Complete    | Local Ollama explanation layer (scoped); graceful fallback if server unreachable |
| `api/schemas.py`                | Complete    | Pydantic response models for all 5 endpoints; discriminated union on `kind` for `TripLeg`/`WalkLeg`; `Literal` types for `risk_label`, `status`, `kind` |
| `api/main.py`                   | Complete    | /routes, /stops, /health, /ingest/gtfs-static endpoints; `response_model=` on all 5 decorators; `_rt_poll_and_observe()` wraps poll + DB observation; daily refresh uses `fill_gaps_only=True` |

---

## End-to-end test results (2026-02-16)

Verified against real GO Transit GTFS data with live GTFS-RT feeds active:

- **904 stops**, 43 routes, 125 245 trips, 2 081 547 stop times ingested
- **Graph**: 904 nodes, 4 017 edges (1 867 trip + 2 150 walk)
- **Routes confirmed working**: `GET /routes?origin=UN&destination=GL` returns
  scored routes in < 2 s
- **Sample result** (Route 1, 08:00 departure):
  Union Station → Bramalea → Brampton → Mount Pleasant → Georgetown → Acton → **Guelph Central** — 1h 31m, risk = Low (0.133)
- **Risk scoring**: 5 439 reliability records seeded; per-bucket priors now
  differentiated (off-peak ~0.13, PM peak ~0.2)
- **GTFS-RT**: all three feeds (TripUpdates, VehiclePosition, Alerts) polling
  live at 30 s intervals; 113 trip update entities parsed on first poll
- **RT observation**: `observe_departures()` called after every poll; real cancellations and delays accumulate into `ReliabilityRecord`; `/health` `reliability.records` grows beyond initial 5 439 seed as observations are written
- **LLM endpoint**: `?explain=true` wired and callable (requires local Ollama; returns graceful fallback message if not running)
- **Test suite**: 117 tests, all passing

### GTFS-RT endpoint URLs (Metrolinx Open API)

Base: `https://api.openmetrolinx.com/OpenDataAPI/api/V1/Gtfs/Feed/`

| Feed             | Path suffix       | Rate limit     |
|------------------|-------------------|----------------|
| Trip Updates     | `TripUpdates`     | 300 req/s      |
| Vehicle Position | `VehiclePosition` | 300 req/s      |
| Service Alerts   | `Alerts`          | 300 req/s      |

API key appended as `?key=<value>`. Protobuf format requires `Accept: application/x-protobuf` header (JSON is the default).

---

## Known TODOs inside the code

| Location | Issue |
|---|---|
| `routing/engine.py` | ~~Incoherent departure times~~ — fixed: `_schedule_path` queries real trips per segment; ~~transfer wait-time stubbed~~ — now enforced in `_passes_filters` |
| `routing/engine.py` | ~~Routes 4–5 use local street stops~~ — ~~fixed: `_passes_filters` now rejects any route containing a zero-second trip leg~~ — **filter removed 2026-02-18**: GTFS 1-minute resolution legitimately produces same-minute consecutive stops on multi-stop corridors; the filter was a false positive; see ADR-009 |
| `routing/engine.py` | ~~**PERF: `/routes` latency ~50 s**~~ — **fixed 2026-02-18**: added `_RouteQueryCache` (per-call memo) + lowered `MAX_CANDIDATES` from 40× to 15×; see Tier 3 backlog. Next step if still slow: bulk-prefetch all candidate trip-select rows in one SQL query before the Yen's loop. |
| `reliability/historical.py` `record_observed_departure()` | Called by `seed_from_static` (synthetic priors) and `observe_departures()` in `ingestion/gtfs_realtime.py` (real RT observations) |
| `api/main.py` `POST /ingest/gtfs-static` | ~~No auth~~ — optional `INGEST_API_KEY` guard added; open when unset |
| `graph/builder.py` `_add_walk_edges()` | O(n²) stop comparison — fine for GO Transit stop count, but add spatial indexing if expanded to full GTA |

---

## Routing engine — implementation notes

The graph stores one edge per `(from_stop, to_stop, route_id)` keeping the
minimum travel time across all trips on that route. This means:

- `nx.shortest_simple_paths` cannot be called on a `MultiDiGraph` → the engine
  first projects to a `DiGraph` (min-weight edge per pair) before running Yen's
- Transfer counting must use **route_id changes**, not trip_id changes (adjacent
  edges on the same route may carry different trip_ids from independent min-time
  selection)
- `MAX_CANDIDATES = max_routes * 15` cap prevents Yen's from hanging when
  walk edges create high-branching alternative paths (lowered from 40× after
  profiling showed most candidates beyond ~50 fail `_passes_filters` immediately)

### Known routing pitfalls (2026-02-18)

**Corridor tie-breaking bug (fixed):** When multiple routes share a stop pair
with identical minimum edge weights (common on shared-corridor stops, e.g. routes
19, 27, 94, and 96 all at `weight=0` for `Yonge @ Poyntz → Yonge @ Florence`),
Python's `min()` could pick a short-haul route (94/96) instead of the long-haul
route (27) that continues to the transfer point. `_schedule_path` would then call
`_find_trip_legs` with the wrong route_id, find no matching trip, and return None
for a valid path.

**Fix:** `_pick_longest_route(G, node_path, start)` helper in `routing/engine.py`
scans forward through the node path and counts how many consecutive stops each
tied candidate route covers. The route with the longest run wins. The segment
extension loop was also updated to check `any(e["route_id"] == route_id ...)` on
the MultiDiGraph rather than relying on the min-weight edge's route_id at each step.

**Zero-second leg filter (removed):** `_passes_filters` originally rejected any
route containing a `travel_seconds == 0` leg, intended to block graph artifacts.
However, GTFS uses 1-minute resolution and legitimate trips on dense urban
corridors (e.g. two consecutive Aquitaine Ave stops both scheduled at `10:06:00`)
produce zero-second legs in the assembled output. Removing the filter allows these
valid routes through. The graph-level `max(0, ...)` guard in `builder.py` still
prevents negative weights.

---

## Architecture Decisions

### ADR-001: Single repo, modular package structure
- **Decision:** Monorepo with packages: `ingestion`, `graph`, `routing`, `reliability`, `llm`, `api`
- **Rationale:** Modules share data models and are sequentially dependent. Separate repos add overhead with no benefit at v1 scope.
- **Date:** 2026-02-10

### ADR-002: SQLite (dev) / PostgreSQL-compatible (prod)
- **Decision:** SQLAlchemy with `DATABASE_URL` env var; defaults to SQLite
- **Rationale:** Zero-ops local development. Switching to PostgreSQL requires only changing `DATABASE_URL`.
- **Date:** 2026-02-10

### ADR-003: Python + FastAPI stack
- **Decision:** Python, FastAPI, networkx, SQLAlchemy, anthropic SDK
- **Rationale:** Best ecosystem for GTFS tooling (`gtfs-realtime-bindings`), data pipelines (`pandas`), graph algorithms (`networkx`), and LLM integration.
- **Date:** 2026-02-10

### ADR-004: LLM scope boundary
- **Decision:** LLM receives structured JSON, outputs plain-language explanation only.
- **Rationale:** LLMs must never generate routes, invent transit data, or override deterministic scoring logic.
- **Date:** 2026-02-10

### ADR-005: GTFS times stored as HH:MM:SS strings
- **Decision:** Store `arrival_time` / `departure_time` as raw strings, not integers.
- **Rationale:** GTFS spec allows values > `24:00:00` for trips crossing midnight. Conversion to seconds-past-midnight happens at the application layer.
- **Date:** 2026-02-10

### ADR-006: Route risk = max leg risk (not sum)
- **Decision:** Overall route risk score = maximum of individual leg risk scores.
- **Rationale:** The weakest link dominates. A route is only as reliable as its riskiest leg. Open to revision once we have real data.
- **Date:** 2026-02-10

### ADR-007: DiGraph projection for Yen's algorithm
- **Decision:** Before calling `nx.shortest_simple_paths`, project the `MultiDiGraph` to a `DiGraph` keeping only the min-weight edge per `(u, v)` pair.
- **Rationale:** `shortest_simple_paths` is decorated `@not_implemented_for("multigraph")` in NetworkX. The projection is cheap and preserves optimal path weights.
- **Date:** 2026-02-11

### ADR-008: Transfer = route_id change, not trip_id change
- **Decision:** Count a transfer whenever `route_id` changes between consecutive trip legs, not when `trip_id` changes.
- **Rationale:** The graph picks the minimum-travel-time trip independently per edge, so consecutive edges on the same route may carry different trip_ids. Counting trip_id changes would falsely produce 10+ transfers on a direct ride.
- **Date:** 2026-02-11

### ADR-009: Remove zero-second leg filter; add longest-route tie-breaking
- **Decision:** Remove the `travel_seconds == 0` filter from `_passes_filters`. Add `_pick_longest_route()` to break ties when multiple routes share identical minimum edge weights on a corridor.
- **Rationale:** Two separate bugs were found when testing `origin=02821` (Yonge @ Poyntz) → `destination=00201` (University of Guelph): (1) the min-weight tie between routes 19/27/94/96 on shared Yonge corridor stops caused the engine to pick a short-haul route that doesn't reach the transfer point; (2) after fixing (1), the zero-second filter rejected the route because GTFS 1-minute resolution produces same-minute stop pairs on dense urban segments — a legitimate data characteristic, not an artifact. Both filters were over-eager for real-world multi-stop corridors.
- **Date:** 2026-02-18

---

## Data Sources

| Feed                      | Format    | Refresh | Config key                        |
|---------------------------|-----------|---------|-----------------------------------|
| GO Transit GTFS Static    | ZIP (CSV) | Daily   | `GTFS_STATIC_URL`                 |
| GTFS-RT Trip Updates      | Protobuf  | 30 s    | `GTFS_RT_TRIP_UPDATES_URL`        |
| GTFS-RT Vehicle Positions | Protobuf  | 30 s    | `GTFS_RT_VEHICLE_POSITIONS_URL`   |
| GTFS-RT Service Alerts    | Protobuf  | 30 s    | `GTFS_RT_ALERTS_URL`              |

> **Feed URLs:** Obtain from the Metrolinx Open Data portal.
> GTFS-RT feeds require an API key.

---

## Open Questions

- [x] Metrolinx GTFS-RT API key — obtained 2026-02-16; feeds live
- [ ] Should route risk = max leg risk, or a weighted sum? Revisit once real RT data accumulates.

---

## Frontend

A web UI is the natural next step.  The backend already exposes a clean,
OpenAPI-documented HTTP API (FastAPI `/docs`).

**Recommendation: separate repository.**

| Concern | Reason |
|---|---|
| Different package manager | Backend uses `uv`; frontend needs npm/bun/yarn |
| Different CI | Python tests vs lint + Vitest + build + preview deployments |
| Different deployment target | Python server vs Vercel / Netlify / Cloudflare Pages |
| Independent release cadence | Frontend iteration shouldn't require a backend deploy and vice versa |
| Type safety | FastAPI generates an OpenAPI spec at `/openapi.json`; `openapi-typescript` or `hey-api` can generate TypeScript types from it — no shared code needed |

A monorepo (`frontend/` subdirectory) only makes sense if this will never be deployed separately and you want a single place to track everything.  Even then, JS tooling artefacts (`node_modules`, `package.json`, lock files) polluting the Python repo is unpleasant.

**Suggested stack for the frontend repo:**

- **Framework**: Next.js (App Router) or SvelteKit — both have excellent fetch patterns and are trivially deployable on Vercel/Netlify
- **Type generation**: `openapi-typescript` against `GET /openapi.json`
- **Map**: Leaflet or MapLibre GL for stop/route visualisation
- **State**: React Query / TanStack Query for route fetching + caching

**Minimum viable features for a v1 frontend:**

- [ ] Origin / destination stop search (calls `GET /stops?query=`)
- [ ] Date + departure time picker
- [ ] Route results list with risk label badges (Low / Medium / High)
- [ ] Leg-by-leg breakdown with departure/arrival times
- [ ] Optional LLM explanation toggle (calls `?explain=true`)

---

## Build Order (v1 — completed)

- [x] Project scaffolding + DB models
- [x] GTFS static ingestion
- [x] Graph construction
- [x] Routing engine (Yen's k-shortest paths, departure-time aware)
- [x] GTFS-RT polling (live 2026-02-16 — Metrolinx API key obtained)
- [x] Historical reliability tracking
- [x] Live risk modifiers
- [x] LLM explanation layer → **switched to local Ollama** (2026-02-11)
- [x] FastAPI endpoints
- [x] End-to-end test with real GTFS data (2026-02-11)
- [x] Route-type filter — zero-second leg filter (2026-02-11)
- [x] Departure-time aware routing (2026-02-11)
- [x] Unit + integration tests — 117 tests (2026-02-16)
- [x] Reliability data seeding from static schedule (2026-02-11)
- [x] Optional auth on ingest endpoints (2026-02-11)
- [x] Route deduplication by trip_id signature (2026-02-11)
- [x] Pydantic response models — `api/schemas.py`; all 5 endpoints typed (2026-02-16)

---

## Post-v1 Backlog

Priority tiers based on impact and dependency on GTFS-RT.

### Tier 1 — Correctness gaps (no external dependency)

- [x] **Daily GTFS static refresh scheduler** — `_daily_gtfs_refresh()` APScheduler interval job (every `GTFS_REFRESH_HOURS`, default 24h); calls `refresh_static_data` + `build_graph` + `seed_from_static(fill_gaps_only=True)` to preserve accumulated RT data; scheduler starts unconditionally (2026-02-11; fill_gaps_only switched 2026-02-16)
- [x] **Chain reliability reseed into static ingest** — `POST /ingest/gtfs-static` calls `seed_from_static(fill_gaps_only=False)` (manual full reseed); `fill_gaps_only=True` used by scheduled refresh to preserve real observations (2026-02-11)
- [x] **Enhanced `/health` endpoint** — includes GTFS data age, graph node/edge counts, reliability record count, last-seeded timestamp, RT polling status (2026-02-11)

### Tier 2 — Quality / developer experience

- [x] **Pydantic response models** — `api/schemas.py` defines typed models for all 5 endpoints; discriminated union (`TripLeg`/`WalkLeg` on `kind`), `Literal` constraints on `risk_label`/`status`; `response_model=` wired on all decorators; `/docs` now shows full schemas (2026-02-16)
- [x] **Surface `transfers` and `total_walk_metres` on `/routes` response** — `count_transfers()` and `total_walk_metres()` helpers in `routing/engine.py`; included in `ScoredRoute` schema and `/routes` response (2026-02-16)
- [~] **GTFS-RT mock state injector** — won't do; Metrolinx API key obtained 2026-02-16, live feeds active

### Tier 3 — Future niceness

- [x] **Later-departure fill** — `_fill_later_departures()` in `routing/engine.py`; round-robins over candidate paths, advancing each path's pointer 1 second past its last known departure until slots are filled or paths exhausted; 132 tests passing (2026-02-17)
- [x] **`GET /stops` — include routes served** — `routes_served: list[str]` added to `StopResult`; fetched via a single `stop_times → trips` join across all matched stops (2026-02-16)
- [x] **Response caching for `/routes`** — module-level dict in `api/main.py`, keyed by `(origin, destination, YYYY-MM-DD, HH:MM)`; caches raw `find_routes()` output only (risk scoring stays fresh); 1-hour TTL + explicit clear on daily refresh and manual ingest; 139 tests passing (2026-02-17)
- [x] **Spatial index for walk edges** — latitude-sorted index + binary search (stdlib `bisect`, no new deps); O(n·k) vs O(n²); Δlon pre-filter gates haversine; `test_matches_brute_force` verifies identical edge sets; ~200× fewer haversine calls at 10 000 stops (2026-02-17)
- [x] **`/routes` latency optimisation** — `_RouteQueryCache` per-call memo in `routing/engine.py`; two levels: (1) trip-select keyed by `(route_id, first_stop, last_stop, date, not_before_sec)` avoids re-running the 4-table JOIN for repeated first segments; (2) stop_times keyed by `trip_id` fetches all stop times for a trip once and filters in Python on subsequent calls; `MAX_CANDIDATES` lowered from 40× to 15×; estimated DB round-trips reduced from ~1 200 to ~50–150 (2026-02-18)
- [x] **`total_travel_seconds` corrected to wall-clock time** — was summing leg durations (hiding multi-hour transfer waits); now `last_trip_arrival − first_trip_departure`; regression test added (2026-02-18)
- [x] **LLM explanation quality** — `_build_llm_payload()` collapses same-trip_id legs, strips IDs, caps at 3 routes; `_normalise_explanation()` injects blank lines between sections; system prompt with numbered rules prevents risk-label overrides and data-format tangents (2026-02-18)

### Tier 4 — Infrastructure / scalability

- [x] **Migrate to PostgreSQL + PostGIS** — completed 2026-02-18. `postgis/postgis:16-3.4-alpine` via Docker Compose; `stops.geog` Geography column (auto-GIST indexed by GeoAlchemy2); `_add_walk_edges` dispatches to `_add_walk_edges_postgis` (ST_DWithin) on PostgreSQL and falls back to `_add_walk_edges_bisect` on SQLite/tests; FK-safe ingestion with `session.flush()` + orphan filtering; 3 PostGIS integration tests in `tests/integration/`; 145 unit tests still pass on SQLite; verified 904 stops / 106 996 trips / 4 017 edges against live PostgreSQL (2026-02-18)

### Unblocked by GTFS-RT API key (2026-02-16)

- [x] **Live risk modifiers in production** — cancellations, vehicle positions, service alerts flowing via `reliability/live.py` and `ingestion/gtfs_realtime.py`
- [x] **Real reliability data accumulation** — `observe_departures()` called after every `poll_all()`; cancelled trips recorded at all stops; in-progress trips recorded per-stop once departure time has passed; `_recorded_today` prevents double-counting; daily refresh uses `fill_gaps_only=True` so accumulated data survives schedule refreshes (2026-02-16)

---

## Environment Setup

```bash
# Install uv: https://docs.astral.sh/uv/getting-started/installation/
uv sync --group dev            # create .venv + install all deps including pytest
cp .env.example .env           # fill in GTFS_RT_API_KEY (optional), Ollama settings

ollama pull llama3.2           # optional: enables ?explain=true

uv run uvicorn api.main:app --port 8000    # start server (no --reload)
curl -X POST http://localhost:8000/ingest/gtfs-static       # first-run (~30s)
curl -X POST http://localhost:8000/ingest/reliability-seed  # seed risk priors
curl "http://localhost:8000/routes?origin=UN&destination=GL"

uv run pytest tests/ -q        # run test suite
```

> **Note:** Do not use `--reload` in development — multiple reloader processes
> can survive `Ctrl+C` and serve stale bytecode. Use a clean restart instead.
