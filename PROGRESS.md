# GO Transit Reliability Router — Progress Tracker

## Architecture Overview

```
GTFS Static (daily)  ──► ingestion/gtfs_static.py ──► SQLite DB
                                                           │
GTFS-RT (30–60s)     ──► ingestion/gtfs_realtime.py        │
                              │ (in-memory state)           │
                              │                             ▼
                              │                    graph/builder.py
                              │                    (networkx DiGraph)
                              │                             │
                              │                             ▼
                              │                    routing/engine.py
                              │                    (top-N by schedule)
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

| Module                          | Status      | Notes                                     |
|---------------------------------|-------------|-------------------------------------------|
| `db/models.py`                  | Complete    | SQLAlchemy models for GTFS + reliability  |
| `db/session.py`                 | Complete    | Engine, SessionLocal, get_session         |
| `ingestion/gtfs_static.py`      | Complete    | Download, parse, store all GTFS CSVs      |
| `ingestion/gtfs_realtime.py`    | **Blocked** | Code complete; awaiting Metrolinx API key (up to 10 business days) |
| `graph/builder.py`              | Complete    | MultiDiGraph; one edge per (stop-pair, route_id); single SQL join query |
| `routing/engine.py`             | Complete    | Yen's k-shortest paths + filters          |
| `reliability/historical.py`     | Complete    | Rolling-window score per route/stop/bucket|
| `reliability/live.py`           | Complete    | Live GTFS-RT risk modifiers               |
| `llm/explainer.py`              | Complete    | Claude explanation layer (scoped)         |
| `api/main.py`                   | Complete    | /routes, /stops, /health endpoints        |

**Blocked:**
- GTFS-RT live feeds blocked on Metrolinx API key — registration submitted, up to 10 business days. Static GTFS feed does not require a key.

**Remaining before first real test:**
- [ ] Run first GTFS static ingest (`POST /ingest/gtfs-static`) — unblocked, no key needed
- [ ] Verify stop IDs for Toronto (Sheppard–Yonge) and Guelph stops
- [ ] Configure `GTFS_RT_API_KEY` in `.env` once key arrives

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
- **Rationale:** Per CLAUDE.md — LLMs must never generate routes, invent transit data, or override deterministic scoring logic.
- **Date:** 2026-02-10

### ADR-005: GTFS times stored as HH:MM:SS strings
- **Decision:** Store `arrival_time` / `departure_time` as raw strings, not integers.
- **Rationale:** GTFS spec allows values > `24:00:00` for trips crossing midnight. Conversion to seconds-past-midnight happens at the application layer.
- **Date:** 2026-02-10

### ADR-006: Route risk = max leg risk (not sum)
- **Decision:** Overall route risk score = maximum of individual leg risk scores.
- **Rationale:** The weakest link dominates. A route is only as reliable as its riskiest leg. Open to revision once we have real data.
- **Date:** 2026-02-10

---

## Data Sources

| Feed                     | Format         | Refresh | Config key                        |
|--------------------------|----------------|---------|-----------------------------------|
| GO Transit GTFS Static   | ZIP (CSV)      | Daily   | `GTFS_STATIC_URL`                 |
| GTFS-RT Trip Updates     | Protobuf       | 30s     | `GTFS_RT_TRIP_UPDATES_URL`        |
| GTFS-RT Vehicle Positions| Protobuf       | 30s     | `GTFS_RT_VEHICLE_POSITIONS_URL`   |
| GTFS-RT Service Alerts   | Protobuf       | 30s     | `GTFS_RT_ALERTS_URL`              |

> **Feed URLs:** Obtain from the Metrolinx Open Data portal.
> GTFS-RT feeds may require an API key.

---

## Open Questions

- [ ] Does Metrolinx require an API key for GTFS-RT feeds?
- [ ] Which specific stop IDs cover the Toronto (Sheppard–Yonge) ↔ Guelph corridor?
- [ ] What is the right max walking radius? (Currently `MAX_WALK_METRES=500`)
- [ ] Should route risk = max leg risk, or a weighted sum? Revisit with real data.
- [ ] How to seed historical reliability before enough data accumulates? (Use 0.8 neutral prior for now.)
- [ ] Should the `/ingest/gtfs-static` endpoint require auth in production?

---

## Build Order (v1)

- [x] Project scaffolding + DB models
- [x] GTFS static ingestion
- [x] Graph construction
- [x] Routing engine
- [x] GTFS-RT polling
- [x] Historical reliability tracking
- [x] Live risk modifiers
- [x] LLM explanation layer
- [x] FastAPI endpoints
- [ ] **First end-to-end test** with real GTFS data
- [ ] Unit tests for routing + reliability modules
- [ ] Reliability data seeding / backfill strategy

---

## Environment Setup

```bash
# Install uv if you haven't already: https://docs.astral.sh/uv/getting-started/installation/
uv sync                        # creates .venv + installs all dependencies
cp .env.example .env           # fill in GTFS_RT_API_KEY + ANTHROPIC_API_KEY when available
uv run uvicorn api.main:app --reload
```
