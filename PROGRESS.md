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
| `ingestion/gtfs_realtime.py`    | **Blocked** | Code complete; awaiting Metrolinx API key                          |
| `graph/builder.py`              | Complete    | MultiDiGraph; one edge per (stop-pair, route_id); single SQL join  |
| `routing/engine.py`             | Complete    | Yen's k-shortest paths on projected DiGraph; MAX_CANDIDATES cap    |
| `reliability/historical.py`     | Complete    | Rolling-window score per route/stop/bucket                         |
| `reliability/live.py`           | Complete    | Live GTFS-RT risk modifiers                                        |
| `llm/explainer.py`              | Complete    | Local Ollama explanation layer (scoped); graceful fallback if server unreachable |
| `api/main.py`                   | Complete    | /routes, /stops, /health, /ingest/gtfs-static endpoints            |

**Blocked:**
- GTFS-RT live feeds blocked on Metrolinx API key — registration submitted.
  Static GTFS feed does not require a key.

---

## End-to-end test results (2026-02-11)

Verified against real GO Transit GTFS data:

- **904 stops**, 43 routes, 125 245 trips, 2 081 547 stop times ingested
- **Graph**: 904 nodes, 4 017 edges (1 867 trip + 2 150 walk)
- **Routes confirmed working**: `GET /routes?origin=UN&destination=GL` returns
  5 scored routes in < 2 s
- **Sample result** (Route 1):
  Union Station → Bramalea → Brampton → Mount Pleasant → Georgetown → Acton → **Guelph Central** — 1h 21m, risk = Low (0.2)
- **Risk scoring**: historical prior (0.8 neutral) + live modifiers applied per
  leg; late-evening departures correctly flagged
- **LLM endpoint**: `?explain=true` wired and callable (requires local Ollama; returns graceful fallback message if not running)

---

## Known TODOs inside the code

| Location | Issue |
|---|---|
| `routing/engine.py` | ~~Incoherent departure times~~ — fixed: `_schedule_path` queries real trips per segment; ~~transfer wait-time stubbed~~ — now enforced in `_passes_filters` |
| `routing/engine.py` | ~~Routes 4–5 use local street stops~~ — fixed: `_passes_filters` now rejects any route containing a zero-second trip leg |
| `reliability/historical.py` `record_observed_departure()` | Called by `seed_from_static` (synthetic) and future GTFS-RT background job (real observations) |
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
- A `MAX_CANDIDATES = max_routes * 20` cap prevents Yen's from hanging when
  walk edges create high-branching alternative paths

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

- [ ] Metrolinx GTFS-RT API key — registration submitted ~2026-02-11, expected ~10 days
- [ ] Should route risk = max leg risk, or a weighted sum? Revisit once real RT data flows in.

---

## Build Order (v1 — completed)

- [x] Project scaffolding + DB models
- [x] GTFS static ingestion
- [x] Graph construction
- [x] Routing engine (Yen's k-shortest paths, departure-time aware)
- [x] GTFS-RT polling (code complete; blocked on API key)
- [x] Historical reliability tracking
- [x] Live risk modifiers
- [x] LLM explanation layer → **switched to local Ollama** (2026-02-11)
- [x] FastAPI endpoints
- [x] End-to-end test with real GTFS data (2026-02-11)
- [x] Route-type filter — zero-second leg filter (2026-02-11)
- [x] Departure-time aware routing (2026-02-11)
- [x] Unit + integration tests — 104 tests (2026-02-11)
- [x] Reliability data seeding from static schedule (2026-02-11)
- [x] Optional auth on ingest endpoints (2026-02-11)
- [x] Route deduplication by trip_id signature (2026-02-11)

---

## Post-v1 Backlog

Priority tiers based on impact and dependency on GTFS-RT.

### Tier 1 — Correctness gaps (no external dependency)

- [x] **Daily GTFS static refresh scheduler** — `_daily_gtfs_refresh()` APScheduler interval job (every `GTFS_REFRESH_HOURS`, default 24h); calls `refresh_static_data` + `build_graph` + `seed_from_static(fill_gaps_only=False)`; scheduler now starts unconditionally (2026-02-11)
- [x] **Chain reliability reseed into static ingest** — `POST /ingest/gtfs-static` now calls `seed_from_static(fill_gaps_only=False)` after rebuilding the graph; `fill_gaps_only=True` mode added for post-GTFS-RT use (2026-02-11)
- [ ] **Enhanced `/health` endpoint** — include GTFS data age, graph node/edge counts, reliability record count, and last-seeded timestamp; currently just returns `{"status": "ok"}`

### Tier 2 — Quality / developer experience

- [ ] **Pydantic response models** — replace `dict[str, Any]` on all endpoints with typed models; gives validated responses, better Swagger UI at `/docs`, and catches field mismatches at dev time
- [ ] **Surface `transfers` and `total_walk_metres` on `/routes` response** — both computable from existing leg data; useful for UI consumers
- [ ] **GTFS-RT mock state injector** — `ingestion/mock_realtime.py` utility to push synthetic cancellations, alerts, and vehicle positions into the in-memory state dicts; lets the full live-risk path be tested and validated before the API key arrives

### Tier 3 — Future niceness

- [ ] **Later-departure fill** — if dedup finds fewer than `max_routes` distinct trip sequences, fill remaining result slots with later departures of already-seen route combinations
- [ ] **`GET /stops` — include routes served** — return which route_ids call at each matching stop
- [ ] **Response caching for `/routes`** — same origin/dest/date/time → skip Yen's; useful once traffic grows
- [ ] **Spatial index for walk edges** — replace O(n²) stop comparison in `graph/builder._add_walk_edges` with an R-tree or k-d tree; not needed at 904 stops but required if scope expands to full GTA

### Blocked on GTFS-RT API key

- [ ] **Live risk modifiers in production** — cancellations, vehicle positions, service alerts; code complete in `reliability/live.py` and `ingestion/gtfs_realtime.py`
- [ ] **Real reliability data accumulation** — `record_observed_departure()` called from RT replay to replace synthetic priors with actual observations

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
