# CLAUDE.md

## Project: Reliable GO Transit Routing (Toronto ↔ Guelph)

This document provides authoritative context, constraints, and design
intent for AI-assisted development of this repository.\
It should be treated as the **source of truth** for architectural
decisions.

------------------------------------------------------------------------

## Problem Statement

GO Transit bus routes between Toronto and Guelph frequently suffer
from: - Bus no-shows despite being marked "on time" - Vague service
alerts (e.g. "operational issues") - Risky transfers with minimal
buffers

Existing routing tools optimize for **scheduled travel time**, not
**real-world reliability**.

This project builds a **reliability-first routing system** that
explicitly models uncertainty and provides fallback plans.

------------------------------------------------------------------------

## Core Design Principle

Algorithms generate routes.\
Probabilistic models score risk.\
LLMs explain decisions.

LLMs must **never**: - Generate routes - Invent transit data - Override
deterministic scoring logic

------------------------------------------------------------------------

## Scope (v1)

### Included

-   Geography: Toronto (Sheppard--Yonge area) ↔ Guelph
-   Transit mode: GO buses only
-   Transfers: Walking + GO
-   Interface: Web API
-   Data: GTFS + GTFS-Realtime

### Excluded

-   TTC routing
-   Mobile applications
-   Machine learning models
-   Full GTA coverage

------------------------------------------------------------------------

## Data Sources

### GTFS (Static)

Used for: - Stops - Routes - Trips - Stop times - Service calendars

Pulled daily.

### GTFS-Realtime

Polled every 30--60 seconds: - Trip updates (delays, cancellations) -
Vehicle positions (optional but useful) - Service alerts

Used to adjust **current-day reliability**, not as absolute truth.

------------------------------------------------------------------------

## Routing Architecture

1.  Build a directed graph:

    -   Nodes: Transit stops
    -   Edges: Scheduled bus trips + bounded walking transfers

2.  Generate top N routes by scheduled travel time.

3.  Filter invalid routes:

    -   Excessive transfers
    -   Unrealistic wait times

4.  Score remaining routes by reliability.

------------------------------------------------------------------------

## Reliability Model

### Historical Reliability (Prior)

Tracked per: - Route - Stop - Time bucket (weekday/weekend, hour range)

Metrics: - % of scheduled departures observed - Average delay -
Cancellation frequency

Stored using rolling windows (14--30 days).

------------------------------------------------------------------------

### Live Risk Modifiers (Likelihood)

Applied on top of historical reliability: - Active service alerts -
Earlier same-day cancellations - Missing vehicle position near departure
time - Late evening / weekend service

Conceptual model:

P(miss_now) = f(historical_risk, live_conditions)

No single signal is treated as definitive.

------------------------------------------------------------------------

## Route Risk Score

Computed as: - Sum of per-leg risks - Transfer penalties - Tight
connection penalties (\<10 minutes)

Outputs: - Numeric risk score (0--1) - Qualitative label: Low / Medium /
High

------------------------------------------------------------------------

## LLM Responsibilities

### Inputs

-   Candidate routes (structured JSON)
-   Per-leg risk scores
-   Active alerts
-   Transfer details

### Outputs

-   Plain-language explanations
-   Tradeoff summaries
-   Explicit fallback instructions

All outputs must be traceable to provided inputs.

------------------------------------------------------------------------

## Non-Goals

-   Predicting exact arrival times
-   Replacing official agency data
-   Real-time turn-by-turn navigation
-   Optimizing for absolute speed

------------------------------------------------------------------------

## Extensibility (Future)

-   TTC integration
-   Weather-based risk modifiers
-   Multi-city support
-   ML-based reliability prediction

The core architecture should remain unchanged.

------------------------------------------------------------------------

## Git Conventions

- Do **not** include `Co-Authored-By: Claude` or any AI attribution lines in commit messages.

------------------------------------------------------------------------

## Current Build State (as of 2026-02-11)

The full v1 pipeline is scaffolded, implemented, and verified against real GO Transit GTFS data.
Static ingest and graph construction are confirmed working. GTFS-RT is code-complete but blocked
pending Metrolinx API key approval. See `PROGRESS.md` for module status, ADR log, and checklist.

### What exists

| File | Purpose | Key notes |
|---|---|---|
| `config.py` | All env-backed config | Load via `python-dotenv`; `GTFS_RT_API_KEY` gates RT polling |
| `db/models.py` | SQLAlchemy ORM | Stop, Route, Trip, StopTime, ServiceCalendar, ServiceCalendarDate, ReliabilityRecord |
| `db/session.py` | Engine + session factory | `init_db()` creates schema; `get_session()` is FastAPI dependency |
| `ingestion/gtfs_static.py` | GTFS ZIP download + parse | `refresh_static_data(session)` is the main entry point; uses `bulk_save_objects` for StopTimes |
| `ingestion/gtfs_realtime.py` | GTFS-RT protobuf polling | **Blocked on API key.** Scheduler only starts when `GTFS_RT_API_KEY` is set; `poll_all()` skips cleanly otherwise |
| `graph/builder.py` | networkx **MultiDiGraph** | One edge per `(from_stop, to_stop, route_id)` keeping min travel time; single SQL join query avoids ORM N+1 on 2M stop times |
| `routing/engine.py` | Route generation | Uses `nx.shortest_simple_paths` (Yen's); `_path_to_legs` picks min-weight edge from MultiDiGraph; **transfer wait-time check is a TODO** |
| `reliability/historical.py` | Rolling-window stats | `get_historical_reliability()` returns 0.8 neutral prior if no data; `classify_time_bucket()` maps datetime → bucket string |
| `reliability/live.py` | Live risk modifiers | `compute_live_risk()` returns `{risk_score, risk_label, modifiers, is_cancelled}`; reads module-level RT state |
| `llm/explainer.py` | Claude explanation | `explain_routes()` sends structured JSON to Claude; system prompt enforces strict scope |
| `api/main.py` | FastAPI app | Lifespan: init_db → build_graph → start scheduler (if key set); endpoints: `GET /routes`, `GET /stops`, `GET /health`, `POST /ingest/gtfs-static` |

### Verified against real data (2026-02-11)

- 904 stops, 43 routes, 125 245 trips, 2 081 547 stop times ingested from Metrolinx CDN
- Graph built with correct edge count (MultiDiGraph, one edge per route per stop pair)
- Static URL requires no API key; `GTFS_RT_API_KEY` blank → scheduler never starts, no errors

### Immediate next steps

1. `uv sync` — creates `.venv` and installs all dependencies
2. `uv run uvicorn api.main:app --reload`
3. `POST /ingest/gtfs-static` to load the dataset and build the graph
4. `GET /stops?query=Guelph` to find stop IDs, then test `GET /routes?origin=X&destination=Y`
5. Fill in `GTFS_RT_API_KEY` in `.env` once Metrolinx approves the registration

### Known TODOs inside the code

- `routing/engine.py` `_passes_filters()`: transfer wait-time check is stubbed with a comment — needs departure-time aware logic once we have a query datetime flowing through
- `reliability/historical.py` `record_observed_departure()`: needs a background job to call it from historical GTFS-RT replay
- `api/main.py` `POST /ingest/gtfs-static`: no auth — add before any production deployment
- `graph/builder.py` `_add_walk_edges()`: O(n²) stop comparison — fine for GO Transit stop count, but add spatial indexing if expanded to full GTA

### Key implementation patterns

- **Package manager**: `uv` — use `uv sync` and `uv run` for all commands; `pyproject.toml` is the source of truth for deps
- **GTFS-RT state** is held in module-level dicts in `ingestion/gtfs_realtime.py` and read directly by `reliability/live.py` — no DB writes for live data
- **Graph is rebuilt** in memory after each static ingest; `get_graph()` raises if called before first build
- **Graph type**: `nx.MultiDiGraph` — allows multiple routes between the same stop pair; routing picks min-weight edge per hop
- **Risk scoring** is per-leg; route risk = `max(leg_risks)` (ADR-006)
- **LLM receives** `routes_with_scores` + `active_alerts` as JSON; never receives raw GTFS or graph data
- **Time buckets**: `weekday_am_peak` (06–09), `weekday_pm_peak` (15–19), `weekday_offpeak`, `weekend`
