# GO Transit Reliability Router

Reliability-first routing for GO bus routes between Toronto and Guelph.

Most routing tools optimise for scheduled travel time. This one models
**real-world reliability** — ranking routes by their likelihood of actually
working, surfacing active alerts, and explaining tradeoffs in plain language.

---

## Problem

GO Transit buses between Toronto and Guelph regularly suffer from:

- Bus no-shows despite showing "on time" in apps
- Vague service alerts ("operational issues")
- Transfers with dangerously tight buffers

## Solution

```
GTFS Static  ──► graph (networkx)  ──► Yen's k-shortest paths
GTFS-RT      ──►  reliability score (historical × live modifiers)
                       └──► local LLM (Ollama) ──► plain-language explanation
```

Routes are generated deterministically. The LLM explains them — it never
generates routes or invents transit data. The explanation layer runs locally
via [Ollama](https://ollama.com) — no API key or cloud account required.

---

## Quickstart

### Docker (recommended)

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env — minimum required: GTFS_RT_API_KEY (Metrolinx Open Data)

# 2. Start the full stack (PostgreSQL + API)
docker compose up -d --build

# 3. Load GTFS data on first boot (~60 s)
curl -X POST http://localhost:8000/ingest/gtfs-static

# 4. Query routes
curl "http://localhost:8000/routes?origin=UN&destination=GL"
```

Watch startup logs: `docker compose logs -f app`

#### AI explanation (`?explain=true`) with Docker

The app container cannot reach `localhost` on the host — `OLLAMA_BASE_URL=http://localhost:11434`
(the default) points to the container's own loopback, where nothing is listening.

Two steps are needed:

**1. Start Ollama bound to all interfaces** (not just loopback):
```bash
# Windows
set OLLAMA_HOST=0.0.0.0
ollama serve

# macOS / Linux
OLLAMA_HOST=0.0.0.0 ollama serve
```

Confirm it is reachable:
```bash
curl http://localhost:11434/api/tags
```

**2. Point `.env` at the host machine:**
```
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=llama3.2
```
`host.docker.internal` is a DNS name Docker Desktop provides that resolves
to the host from inside any container (available on Windows and macOS by default;
on Linux add `--add-host=host.docker.internal:host-gateway` to the compose file).

Then pull the model and restart the app:
```bash
ollama pull llama3.2
docker compose restart app
```

Test:
```bash
curl "http://localhost:8000/routes?origin=UN&destination=GL&explain=true"
```

If `explanation` in the response is a fallback message, check the app logs:
```bash
docker compose logs app | grep -i ollama
```

---

### Local (no Docker)

```bash
# 1. Install dependencies
uv sync

# 2. Configure environment
cp .env.example .env

# 3. (Optional) Set up local LLM for ?explain=true
#    Install Ollama from https://ollama.com, then:
ollama pull llama3.2
ollama serve   # OLLAMA_BASE_URL default (localhost:11434) works fine outside Docker

# 4. Start the API (SQLite — no database setup needed)
uv run uvicorn api.main:app --port 8000

# 5. Load GTFS data (first run only; ~30 s)
curl -X POST http://localhost:8000/ingest/gtfs-static

# 6. Query routes
curl "http://localhost:8000/routes?origin=UN&destination=GL"
```

---

## API

### `GET /routes`

Return up to N reliability-scored routes between two stops.

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `origin` | yes | — | GTFS `stop_id` of departure stop |
| `destination` | yes | — | GTFS `stop_id` of arrival stop |
| `departure_time` | no | now | Earliest departure as `HH:MM` or `HH:MM:SS` |
| `travel_date` | no | today | Travel date as `YYYY-MM-DD` |
| `explain` | no | `false` | `true` to include local LLM explanation (requires Ollama) |

**Responses:**
- `200` — routes found; body contains `routes` array (+ optional `explanation` string)
- `404` — unknown stop ID, or no routes exist between the stops
- `422` — invalid parameter format

**Example response:**
```json
{
  "routes": [
    {
      "legs": [
        {
          "kind": "trip",
          "from_stop_name": "Union Station GO",
          "to_stop_name": "Bramalea GO",
          "departure_time": "16:22:00",
          "arrival_time": "16:49:00",
          "route_id": "01260426-GT",
          "risk": { "risk_score": 0.2, "risk_label": "Low", "modifiers": [] }
        }
      ],
      "total_travel_seconds": 4860,
      "risk_score": 0.2,
      "risk_label": "Low"
    }
  ]
}
```

Each leg `risk` object contains:

| Field | Type | Description |
|-------|------|-------------|
| `risk_score` | float 0–1 | Combined historical + live risk (higher = riskier) |
| `risk_label` | string | `Low` (< 0.33) / `Medium` (< 0.66) / `High` |
| `modifiers` | list[str] | Human-readable notes (alerts, cancellations, late evening, etc.) |
| `is_cancelled` | bool | `true` if the trip is currently marked cancelled in GTFS-RT |

---

### `GET /stops`

Search stops by name substring. Use this to find `stop_id` values.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `query` | yes | Name substring (min 2 characters) |

**Responses:** `200` with array of `{stop_id, stop_name, lat, lon}` objects; empty array if no match.

```bash
curl "http://localhost:8000/stops?query=Guelph"
curl "http://localhost:8000/stops?query=Union"
```

---

### `GET /health`

Liveness and data-freshness check.

```json
{
  "status": "ok",
  "timestamp": "2026-02-11T10:00:00",
  "gtfs": {
    "stops": 904,
    "trips": 125245,
    "latest_service_date": "20260601",
    "graph_nodes": 904,
    "graph_edges": 4017,
    "graph_built": true,
    "last_built_at": "2026-02-11T09:30:00",
    "next_refresh_at": "2026-02-12T09:30:00"
  },
  "reliability": {
    "records": 1234,
    "last_seeded_at": "2026-02-11T09:35:00"
  },
  "gtfs_rt": {
    "polling_active": false
  }
}
```

All counts are `0` (and timestamps `null`) before `/ingest/gtfs-static` has been called.

---

### `POST /ingest/gtfs-static`

Trigger a full GTFS static data refresh and graph rebuild. Runs automatically
on a daily schedule; call manually after first install.

If `INGEST_API_KEY` is set, the request must include `X-API-Key: <key>`.

**Responses:** `200 {"status": "ok", ...}` on success; `401` if key is wrong/missing.

---

### `POST /ingest/reliability-seed`

Seed the reliability database from the static GTFS schedule. No GTFS-RT
feed required. Uses synthetic per-bucket priors derived from schedule
density (see [Risk model](#risk-model)).

Run this once after `/ingest/gtfs-static` so that risk scores reflect
real route/time-of-day patterns rather than the flat 0.8 neutral prior.

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `window_days` | no | `14` | Days of schedule to sample (1–90) |

**Responses:** `200 {"status": "ok", "records_written": N, ...}` on success; `409` if no GTFS data loaded yet.

```bash
# Seed with default 14-day window
curl -X POST http://localhost:8000/ingest/reliability-seed

# Seed using 30 days for a broader sample
curl -X POST "http://localhost:8000/ingest/reliability-seed?window_days=30"
```

---

## Key stop IDs (Toronto ↔ Guelph corridor)

| Stop | stop_id |
|------|---------|
| Union Station GO | `UN` |
| Bloor GO | `BL` |
| Bramalea GO | `BE` |
| Brampton Innovation District GO | `BR` |
| Mount Pleasant GO | `MO` |
| Georgetown GO | `GE` |
| Acton GO | `AC` |
| Guelph Central GO | `GL` |
| Kitchener GO | `KI` |

---

## Risk model

Risk is scored per leg, then the **maximum leg risk** is used as the route
risk (ADR-006 — the weakest link dominates).

**Historical prior** — rolling 14–30 day window per route / stop / time
bucket:
- `weekday_am_peak` (06:00–09:00)
- `weekday_pm_peak` (15:00–19:00)
- `weekday_offpeak`
- `weekend`

**Live modifiers** (applied on top of historical prior):
- Active service alert for this route or stop
- Same-day cancellation on this trip
- Missing vehicle position near departure
- Late-evening departure (after 22:00)

Output: `risk_score` (0–1) + `risk_label` (Low / Medium / High)

---

## Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///data/transit.db` | SQLite for dev; set to PostgreSQL for prod |
| `GTFS_STATIC_URL` | Metrolinx CDN | URL of GO GTFS ZIP |
| `GTFS_RT_API_KEY` | *(blank)* | Metrolinx Open Data API key; RT polling disabled if unset |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server URL; required for `?explain=true` |
| `OLLAMA_MODEL` | `llama3.2` | Model to use (must be pulled first: `ollama pull <model>`) |
| `MAX_ROUTES` | `5` | Max candidate routes returned |
| `MAX_TRANSFERS` | `2` | Hard cap on route changes |
| `MIN_TRANSFER_MINUTES` | `10` | Minimum transfer buffer |
| `MAX_WALK_METRES` | `500` | Walking transfer radius |

---

## Project structure

```
transit_planner/
├── api/
│   └── main.py              FastAPI app, lifespan, endpoints
├── db/
│   ├── models.py            SQLAlchemy ORM (GTFS + reliability)
│   └── session.py           Engine, SessionLocal, get_session
├── graph/
│   └── builder.py           networkx MultiDiGraph construction
├── ingestion/
│   ├── gtfs_static.py       GTFS ZIP download + parse
│   └── gtfs_realtime.py     GTFS-RT protobuf polling (APScheduler)
├── reliability/
│   ├── historical.py        Rolling-window reliability stats
│   └── live.py              Live GTFS-RT risk modifiers
├── llm/
│   └── explainer.py         Local Ollama explanation layer
├── routing/
│   └── engine.py            Yen's k-shortest paths + risk filters
├── config.py                All env-backed configuration
├── pyproject.toml           Dependencies (managed with uv)
└── .env.example             Environment variable template
```

---

## Known limitations (v1)
- **Stop-level routing only** — no within-stop platform logic.
- **GO buses only** — TTC, Brampton Transit, etc. are excluded from routing
  but their stops may appear in the graph via walk edges.
- **Single uvicorn worker** — APScheduler runs in-process; scaling to multiple
  workers would require moving the scheduler to a separate process.

---

## Data sources

| Feed | Format | Refresh |
|------|--------|---------|
| [GO Transit GTFS Static](https://www.metrolinx.com/en/go-transit/about-go-transit/open-data) | ZIP (CSV) | Daily |
| GTFS-RT Trip Updates | Protobuf | 30 s |
| GTFS-RT Vehicle Positions | Protobuf | 30 s |
| GTFS-RT Service Alerts | Protobuf | 30 s |
