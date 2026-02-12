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

```bash
# 1. Install dependencies
uv sync

# 2. Configure environment
cp .env.example .env
# Edit .env: fill in GTFS_RT_API_KEY once Metrolinx approves registration (optional)
# OLLAMA_BASE_URL and OLLAMA_MODEL have sensible defaults — no API key needed

# 3. (Optional) Set up local LLM for ?explain=true
#    Install Ollama from https://ollama.com, then:
ollama pull llama3.2

# 4. Start the API
uv run uvicorn api.main:app --port 8000

# 5. Load GTFS data (first run only; ~30s)
curl -X POST http://localhost:8000/ingest/gtfs-static

# 6. Query routes
curl "http://localhost:8000/routes?origin=UN&destination=GL"

# 7. Query with explanation (requires Ollama running)
curl "http://localhost:8000/routes?origin=UN&destination=GL&explain=true"
```

---

## API

### `GET /routes`

Return up to N reliability-scored routes between two stops.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `origin` | yes | GTFS `stop_id` of departure stop |
| `destination` | yes | GTFS `stop_id` of arrival stop |
| `departure_time` | no | Earliest departure as `HH:MM` or `HH:MM:SS`. Defaults to current time. |
| `travel_date` | no | Travel date as `YYYY-MM-DD`. Defaults to today. |
| `explain` | no | `true` to include LLM plain-language explanation |

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

### `GET /stops?query=<name>`

Search stops by name substring. Use this to find `stop_id` values.

```bash
curl "http://localhost:8000/stops?query=Guelph"
curl "http://localhost:8000/stops?query=Union"
```

### `GET /health`

Liveness check.

### `POST /ingest/gtfs-static`

Trigger a full GTFS static data refresh and graph rebuild. Runs automatically
on a daily schedule; call manually after first install.

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
│   └── explainer.py         Claude explanation layer
├── routing/
│   └── engine.py            Yen's k-shortest paths + risk filters
├── config.py                All env-backed configuration
├── pyproject.toml           Dependencies (managed with uv)
└── .env.example             Environment variable template
```

---

## Known limitations (v1)
- **GTFS-RT blocked** — awaiting Metrolinx API key approval. All risk
  scores currently use historical priors only.
- **Stop-level routing only** — no within-stop platform logic.
- **GO buses only** — TTC, Brampton Transit, etc. are excluded from routing
  but their stops may appear in the graph via walk edges.

---

## Data sources

| Feed | Format | Refresh |
|------|--------|---------|
| [GO Transit GTFS Static](https://www.metrolinx.com/en/go-transit/about-go-transit/open-data) | ZIP (CSV) | Daily |
| GTFS-RT Trip Updates | Protobuf | 30 s |
| GTFS-RT Vehicle Positions | Protobuf | 30 s |
| GTFS-RT Service Alerts | Protobuf | 30 s |
