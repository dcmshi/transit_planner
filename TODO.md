# TODO — audit backlog

Updated 2026-07-10 (seventh audit pass).  Items ordered by priority within
each section; nothing here is blocking the current live stack.  See
`PROGRESS.md` for everything completed in earlier passes.

## Correctness

### ✅ Timezone handling — schedule times vs container UTC (done 2026-07-10)

> Fixed: `AGENCY_TZ` (`America/Toronto`, overridable via `AGENCY_TZ` env) in
> `config.py`; `/routes` defaults and `query_dt` now agency-local;
> `_parse_scheduled_at` returns agency-anchored aware datetimes; service-day
> rollover in `observe_departures` and the seed window use the agency date.
> `tests/conftest.py` added so the unit suite no longer inherits `.env`'s
> `DATABASE_URL`.  Regression tests in `TestTimezoneHandling`.

GTFS times are America/Toronto local, but the code mixes naive
`datetime.now()` (container-local = UTC in Docker) and UTC with them:

- `api/main.py` `get_routes`: default `departure_time`/`travel_date` use
  `datetime.now()` — inside Docker that's UTC, so "routes from now" is
  4–5 hours ahead of Toronto time.
- `ingestion/gtfs_realtime.py` `_parse_scheduled_at`: treats the local
  schedule time as UTC, then `observe_departures` compares against
  `datetime.now(timezone.utc)` — the "has this departure passed" check
  fires ~4 h early (EDT), so delay observations are recorded for trips
  that haven't left yet.

Fix: add a `TIMEZONE = ZoneInfo("America/Toronto")` config constant and use
it for every point where "now" is compared against schedule times.

### ✅ Risk is scored at query time, not travel time (done 2026-07-10)

> Fixed: `_score_routes_blocking` derives each leg's scheduled datetime from
> the travel date + GTFS departure time (with >24:00:00 rollover) and uses it
> for the historical bucket; `compute_live_risk` takes a `scheduled_dt` param
> keying the weekend bump and the missing-vehicle window to the travel day
> (full-datetime comparison).  Regression tests in `test_reliability.py` and
> `test_api.py`.

`_score_routes_blocking` (`api/main.py`) uses `query_dt = datetime.now()`
for all scoring, ignoring the requested `travel_date`/`departure_time`:

- Historical bucket: `classify_time_bucket(query_dt)` — querying at 10:00
  for a 17:00 departure scores the leg with `weekday_offpeak` history
  instead of `weekday_pm_peak`.
- Weekend bump (`reliability/live.py`): keyed on `query_dt.weekday()` —
  querying Friday for Saturday travel misses the bump; querying Saturday
  for Monday travel wrongly gets it.
- Missing-vehicle modifier: compares seconds-past-midnight only, so a
  query for *tomorrow* 15 minutes from now wrongly applies "no vehicle
  position data" to a trip that can't have a vehicle yet.

Fix: classify the bucket per leg from its scheduled departure + travel
date; gate the vehicle-position check on travel_date == today.

### ✅ Routing hard-assumes `service_id` == YYYYMMDD — now validated (done 2026-07-10)

> Mitigated: `_validate_service_id_convention` in `ingestion/gtfs_static.py`
> aborts the ingest (pre-commit, so previous data survives) when *no*
> service_id parses as YYYYMMDD, and warns when isolated values don't.
> Full ServiceCalendar-based service-date resolution remains future work if
> the feed ever actually changes convention.

`_find_trip_legs` filters trips with `t.service_id = :service_date` and only
honours `calendar_dates` `exception_type = 2` (removed).  `calendar.txt`
(ServiceCalendar — day-of-week patterns + start/end dates) is ingested but
never read, and `exception_type = 1` (added service) is ignored.  This works
for the current GO feed (one service_id per date) but breaks silently if
Metrolinx ever switches to standard weekly service_ids.  At minimum, validate
the date-shaped assumption at ingest time and fail loudly; ideally resolve
service dates through ServiceCalendar + calendar_dates properly.

## Reliability model

### ✅ No-show detection — the headline feature isn't measured (done 2026-07-10)

> Fixed: `record_no_shows` in `ingestion/gtfs_realtime.py` sweeps today's
> schedule (throttled to every 5 min) for trips whose entire run finished
> ≥30 min ago inside continuous RT coverage (`_polling_since`, reset on
> total feed failure) with no appearance in `_seen_in_rt_today`; each stop
> is recorded `was_missed=True` (scheduled += 1, observed += 0), sharing
> the ObservedTrip dedup markers.  Known gap: >24:00:00 final departures
> are never swept (GO corridor service ends before midnight).

`observe_departures` only records trips that *appear* in the RT feed
(cancelled or delayed).  A bus that silently never runs — the exact failure
mode in the README's problem statement — produces no TripUpdate, so nothing
is recorded and `observed_rate` never drops.  Add a sweep that compares the
static schedule against `trip_updates`/`vehicle_positions`: a scheduled
departure with no RT evidence by departure + grace period gets recorded as
a miss (scheduled += 1, observed += 0).

### Rolling window is not enforced

`ReliabilityRecord` counters accumulate forever; `window_start/end_date` are
recorded but nothing ever ages data out.  `WINDOW_DAYS = 14` and `_BUCKETS`
in `reliability/historical.py` are dead constants (delete or use).  Options:
periodic decay job (multiply counters by α < 1 daily), or store per-day rows
and aggregate over the window at read time.  Without this, one bad month
permanently depresses a route's score.

### Live delay data is captured but unused

`TripUpdateState.delay_seconds` and `stop_time_overrides` are polled every
30 s but never influence risk scoring or the response.  Two uses:

- Add an `expected_departure`/`expected_arrival` (scheduled + live delay)
  to trip legs on `/routes`.
- Bump `risk_score` when the incoming trip is already running late
  (e.g. tiered: +0.05 at 5 min, +0.15 at 15 min).

Related open question from PROGRESS.md: revisit route risk = max leg risk
vs a weighted sum once enough real observations accumulate.

## API & operations

### Rate limiting on public endpoints

No rate limiting exists on `/routes` or `/stops`.  The route cache absorbs
repeated identical queries, but unique route queries each cost real CPU
(Yen's + scheduling), so a hostile client could exhaust the worker pool.
Only matters if the API faces the public internet.

- Suggested: [slowapi](https://github.com/laurentS/slowapi) middleware,
  per-IP, e.g. 100 req/min on `/routes`, looser on `/health`.
- `/ingest/*` is already gated by `INGEST_API_KEY` when set.

### Bound the route cache; consider negative caching

`_routes_cache` (`api/main.py`) grows until the daily refresh clears it —
expired entries are only evicted when their exact key is looked up again,
so unique queries accumulate for up to 24 h.  Cap it (LRU or max-entries
sweep).  Also: empty results are never cached, so repeated 404 queries
(unknown stop pairs) recompute every time — a short negative-cache TTL
would close that gap alongside rate limiting.

### Move `/ingest/gtfs-static` to a background task

The endpoint holds the HTTP request open for the full ~60 s ingest and has
no guard against two concurrent ingests racing.  Return `202` + a job id,
run via the existing scheduler/`asyncio.to_thread`, and expose status on
`/health` (or a `/ingest/status`).  Take a simple in-process lock so manual
ingest and the daily refresh can't overlap.

### Surface GTFS-RT feed freshness in `/health`

`_last_fetched`, `_consecutive_poll_failures`, and `_backoff_until` exist in
`ingestion/gtfs_realtime.py` but `/health` only reports `polling_active`.
An operator can't tell that all three feeds have been failing for 30 min.
Add `last_fetched_at`, `consecutive_failures`, and `backing_off_until`.

### `GET /alerts` endpoint

Active service alerts are only visible today via `?explain=true` or leg
modifiers.  A thin read-only endpoint over `service_alerts` (header,
description, affected routes/stops, fetched_at) would let a frontend show
a banner without requesting routes.

### Security niceties

- `_require_ingest_key` uses `!=`; switch to `secrets.compare_digest`.
- Gemini key is passed as a `?key=` URL query param (`llm/explainer.py`);
  use the `x-goog-api-key` header instead so the key can't leak into URL
  logging anywhere.

## Developer experience

### CI

No `.github/workflows/` — the 295-test suite only runs when someone
remembers to.  Minimum: GitHub Actions job with `uv sync` + `pytest`
(SQLite unit suite).  Nice-to-have: second job running
`tests/integration/` against a `postgis/postgis` service container.

### Linter / formatter / type checker

No ruff/mypy/formatter config anywhere.  Suggested: `ruff check` + `ruff
format` (config in `pyproject.toml`), wired into CI.  mypy optional —
the codebase is already well-annotated, so it's mostly free coverage.

### `requirements.txt` is stale — delete it

It contradicts `pyproject.toml`: lists `anthropic` (no longer used —
explainer is Ollama/Gemini via httpx) and omits `psycopg`, `geoalchemy2`,
`shapely`.  uv + `uv.lock` is the source of truth; delete the file or
generate it with `uv export` if something still needs it.

### Split `api/main.py` into modules

~600 lines mixing lifespan/scheduler setup, route-cache helpers (incl.
single-flight locks), and all endpoint handlers.  Readable as-is — purely
cosmetic.  Natural split if it grows further:

- `api/cache.py` — `_routes_cache`, TTL, single-flight locks
- `api/lifespan.py` — startup/shutdown, scheduled jobs
- `api/routes.py` — endpoint handlers (incl. `_score_routes_blocking`)

### Schema migration for other existing deployments

This machine's Docker DB was migrated in place on 2026-06-10.  Any *other*
pre-existing PostgreSQL volume needs (or a `docker compose down -v` reset):

```sql
ALTER TABLE reliability_records ADD COLUMN IF NOT EXISTS source VARCHAR NOT NULL DEFAULT 'seed';
CREATE INDEX IF NOT EXISTS ix_reliability_route_stop_bucket
  ON reliability_records (route_id, stop_id, time_bucket);
DROP INDEX IF EXISTS ix_reliability_records_route_id;
DROP INDEX IF EXISTS ix_reliability_records_stop_id;
DROP INDEX IF EXISTS ix_reliability_records_time_bucket;
```

(`observed_trips` is a new table — `init_db()`/`create_all` adds it
automatically.)  If the project ever needs regular schema changes, consider
adopting Alembic instead of manual SQL.

## Docs

### Config docs drift

- `.env.example` is missing `MAX_WALK_METRES` and `WALK_SPEED_KPH`
  (both read by `config.py`).
- README's configuration table omits `WALK_SPEED_KPH`,
  `GTFS_REFRESH_HOURS`, `GTFS_RT_POLL_SECONDS`, `INGEST_API_KEY`, and
  `CORS_ORIGINS`.

One sync pass over `config.py` → `.env.example` → README.

## Performance (later)

### Ingest memory footprint

`parse_and_store` materialises the full stop_times CSV as pandas → dicts →
~2M ORM objects in a list before one `bulk_save_objects`.  Works today but
peaks at multiple GB.  Chunked inserts (e.g. 50k rows per flush, or
`session.execute(insert(StopTime), dicts)` in batches) would flatten the
spike.

### Batch historical reliability lookups

`_score_routes_blocking` calls `get_historical_reliability` once per trip
leg — up to ~5 routes × several legs of point queries per request.  One
`IN`-query over all (route_id, stop_id, bucket) triples for the request,
then a dict lookup per leg.
