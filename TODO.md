# TODO — audit backlog

Updated 2026-07-10 (eighth audit pass — full source re-read plus a live
check against the running Docker stack and the frontend's new Playwright
e2e suite).  Items ordered by priority within each section; see
`PROGRESS.md` for everything completed in earlier passes.

## Eighth pass — open findings (2026-07-10)

### ✅ Routing-engine batch (fixed 2026-07-10)

> Three fresh-sweep findings: `_rank_routes_by_coverage` now considers
> every trip route on a segment (not just min-weight ties), so a schedule
> period with different run times still falls back to the route that has
> service; `_find_trip_legs` retries up to 5 later departures when the
> earliest match is an express/short-turn variant skipping an intermediate
> stop; and the graph + Yen's projection now swap atomically as one tuple
> (`get_graphs()`), so a request can no longer pair builds across a rebuild
> (which returned an empty result that the negative cache then amplified).

### ✅ Observation-pipeline batch (fixed 2026-07-10)

> Four adversarial-review findings fixed together: no-show coverage now
> requires the trip-updates feed specifically (a gap there resets
> `_polling_since` even while other feeds succeed); the RT evidence set
> rolls at agency midnight inside `poll_all` *before* being updated;
> dedup markers are keyed by the trip's service date with a
> yesterday+today retention window (late-evening trips lingering in the
> feed past midnight were double-counted nightly); and the no-show sweep
> and reliability seeder both exclude `calendar_dates exception_type=2`
> removals like the routing query always did.

### ✅ Unit tests hit the live Metrolinx RT API when `.env` has a key  [HIGH] (fixed 2026-07-10)

> `tests/conftest.py` hard-pins `GTFS_RT_API_KEY=""` before config import,
> and the `client` fixture additionally patches `api.main.GTFS_RT_API_KEY`
> so no future config path can reintroduce network I/O in unit tests.

The `client` fixture in `tests/test_api.py` patches `api.main.init_db`,
`build_graph`, and `SessionLocal`, but the lifespan's
`await _rt_poll_and_observe()` still runs whenever `GTFS_RT_API_KEY` is
set — and `tests/conftest.py` pins `DATABASE_URL` and
`RATE_LIMIT_PER_MINUTE` but **not** the RT key, so a developer `.env`
leaks in.  Consequences, verified on this machine:

- `TestHealth::test_gtfs_rt_freshness_fields_present` fails (asserts
  `trip_updates == 0`, live feed currently has 1) — the suite is red
  locally while green in CI, purely because CI has no `.env`.
- Every `client`-fixture test fires 3 real HTTP requests at the
  rate-limited Metrolinx API (dozens per suite run).

Fix (verified: the failing test passes with the key pinned empty):
`os.environ.setdefault("GTFS_RT_API_KEY", "")` in `tests/conftest.py`,
same pattern and rationale as the existing `DATABASE_URL` pin.  Belt and
braces: also patch `api.main.poll_all` in the `client` fixture so no
future config path can reintroduce network I/O in unit tests.

### ✅ Reliability decay is a no-op below ~10 counts  [MEDIUM-HIGH] (fixed 2026-07-10)

> Counter columns are now Float; decay multiplies without ROUND/CAST so it
> applies at every magnitude, and rows fading below 0.5 scheduled
> departures are purged (scoring falls back to the neutral prior — the
> same `_MIN_SCHEDULED` guard applies at read time).  Live Docker DB
> migrated in place; ALTER SQL recorded in the migration section below.

`decay_reliability_records` scales integer counters with
`CAST(ROUND(x * :f) AS INT)` where `f = 0.5 ** (1/14) ≈ 0.9517`.  Any
counter value `v` with `v · (1 − f) < 0.5` — i.e. **every value ≤ 10** —
rounds back to itself and never decays; larger counters decay down to
~10 and freeze there.  The half-life therefore does not apply to exactly
the sparse-data case it was built for: a record with
`scheduled=1, observed=0` (one recorded no-show) scores risk 1.0
permanently, and seeded counters converge to ~10 instead of fading.
Options: make the counter columns Float (simplest — `_score_record`
already divides), carry the fractional remainder in a new column, or
store per-day rows and aggregate over the window at read time.

### ✅ RT snapshot dicts are read from worker threads while the poller mutates them  [MEDIUM] (fixed 2026-07-10)

> Copy-on-read at every consumer iteration point (`_alerts_for`,
> `_same_route_cancellations`, `/alerts`, the explain payload):
> `list(...)` snapshots are single C-level ops, atomic under the GIL, so
> a poll landing mid-request can no longer raise "changed size during
> iteration".  Chosen over rebind-and-swap to keep the by-name imports
> (and ~20 existing test patches) intact.  Bonus fix: live-signal gating
> now keys on the *service* day, so >24:00:00 legs of today's service
> keep their cancellation/delay signals past midnight.

`_score_routes_blocking` runs in `asyncio.to_thread` and, via
`compute_live_risk` → `_same_route_cancellations` /` _alerts_for`,
iterates `trip_updates` / `service_alerts` while `poll_trip_updates` /
`poll_service_alerts` do `clear()` + `update()/extend()` on the event
loop.  A poll landing mid-iteration raises
`RuntimeError: dictionary changed size during iteration` → a 500 on
`/routes` (and `/alerts` — a sync-def endpoint running in the
threadpool — can serve a momentarily empty list).  Low probability per
request but guaranteed eventually at 30 s polling under load.  Fix:
have pollers build the new dict and swap via a module-level holder
object (readers grab a reference snapshot), or guard both sides with a
lock, or iterate over `list(...)` copies inside the scoring path.

### ✅ `/routes` returns strictly dominated routes  [MEDIUM] (fixed 2026-07-10)

> `_prune_dominated` in `api/main.py` drops any scored route that departs
> no later, arrives no earlier, with no fewer transfers and no lower risk
> than another (ties keep both), then sorts survivors by arrival time —
> the response order is now earliest-arrival, not Yen's path weight.

Live example (Sat 2026-07-11, GL → UN, departure 09:00): all five
results depart 16:08; #1 arrives 17:35 with 0 transfers, #2–#5 all
arrive 20:35 with 2 transfers — four results that are worse than #1 on
every axis.  Dedup is by trip signature only; there is no dominance
pruning.  Drop any route whose (departure ≥, arrival ≥, transfers ≥,
risk ≥) another's, and consider sorting the survivors by arrival time —
today's order is Yen's path weight, so the UI's "#1" is not necessarily
the earliest or the best.  Follow-up: with dominated routes pruned,
`_fill_later_departures` would naturally surface later departures as
the extra options, which is far more useful to a rider.

### Smaller findings  [LOW] — mostly fixed 2026-07-10

- ✅ `search_stops` now escapes `%`/`_` (literal matching).
- ✅ `get_session()` annotated `Iterator[Session]`; engine pool sized for
  the threaded access pattern (`pool_size=15, max_overflow=25`) with
  `pool_pre_ping` for DB-restart recovery.
- ✅ Parser robustness: duplicate PKs deduplicated per feed file; blank
  coordinates / exception_type / stop times skip-and-warn instead of
  aborting; calendar tables cleared unconditionally so a feed that drops
  `calendar_dates.txt` can't leave stale exceptions suppressing trips.
- ✅ Seed buckets: >24:00:00 departures roll onto the next day
  (matching the scorer) instead of `% 24` landing in an unread bucket.
- ✅ LLM payload sanitises all feed-sourced strings (stop names,
  journey), not just alert headers.
- Open (by design / deferred):
  - `total_travel_seconds` excludes leading/trailing walk legs from the
    door-to-door duration (`routing/engine.py`) — semantics change,
    revisit with the frontend.
  - LLM explanations are never cached — a small TTL cache keyed on the
    scored-route signature would do.
  - Rate limiting keys on `request.client.host`; behind a reverse proxy
    all callers share one bucket — revisit with X-Forwarded-For if ever
    proxied.
  - Decay uses a fixed `days_elapsed=1.0`; days the daily job doesn't
    run simply don't decay (stretches the effective window slightly).

### Verified healthy in this pass

- 337/338 unit tests pass (the 1 failure is the RT-key leak above);
  ruff clean.
- Live stack: `/health` reports graph built (889 nodes / 3,955 edges),
  5,198 seeded reliability records; `/stops` and `/routes` exercised
  end-to-end by the frontend's new Playwright suite (5/5 passing) —
  stop search, route planning with risk badges, selection, persistence.
- Timezone handling, travel-time risk keying, no-show sweep, decay
  scheduling, cache bounding, 202 ingest, and the security items from
  passes 1–7 all re-read clean; their regression tests pass.

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

### ✅ Rolling window is not enforced (done 2026-07-10)

> Fixed with exponential decay: `decay_reliability_records` in
> `reliability/historical.py` scales all four counters by
> `0.5 ** (days / WINDOW_DAYS)` once per agency day, called from the daily
> refresh before the gap-fill reseed.  Uniform scaling preserves the score;
> new observations weigh more against the shrunken denominators.  Dead
> `_BUCKETS` constant removed; `WINDOW_DAYS` now meaningful (half-life).

`ReliabilityRecord` counters accumulate forever; `window_start/end_date` are
recorded but nothing ever ages data out.  `WINDOW_DAYS = 14` and `_BUCKETS`
in `reliability/historical.py` are dead constants (delete or use).  Options:
periodic decay job (multiply counters by α < 1 daily), or store per-day rows
and aggregate over the window at read time.  Without this, one bad month
permanently depresses a route's score.

### ✅ Live delay data is captured but unused (done 2026-07-10)

> Fixed: `get_live_delay` (stop override → trip delay) in
> `reliability/live.py`; tiered risk bumps (+0.05 at ≥5 min, +0.15 at
> ≥15 min); `live_delay_seconds` / `expected_departure` /
> `expected_arrival` on same-day trip legs.  Bonus regression fix: all
> per-trip live signals (cancellation, same-route cancellations, delay)
> are now gated to same-day queries — trip_ids repeat across service days,
> so today's cancellation no longer marks tomorrow's run.

`TripUpdateState.delay_seconds` and `stop_time_overrides` are polled every
30 s but never influence risk scoring or the response.  Two uses:

- Add an `expected_departure`/`expected_arrival` (scheduled + live delay)
  to trip legs on `/routes`.
- Bump `risk_score` when the incoming trip is already running late
  (e.g. tiered: +0.05 at 5 min, +0.15 at 15 min).

Related open question from PROGRESS.md: revisit route risk = max leg risk
vs a weighted sum once enough real observations accumulate.

## API & operations

### ✅ Rate limiting on public endpoints (done 2026-07-10)

> Implemented as a dependency-free per-IP sliding window (`_rate_limit` in
> `api/main.py`, `RATE_LIMIT_PER_MINUTE` env, default 100/min, 0 disables)
> on `/routes` and `/stops`; `/health` and `/ingest/*` (API-key-gated)
> exempt.  In-process state is correct for the single-worker deployment;
> revisit (slowapi + shared store) if the app ever scales to multiple
> workers.

### ✅ Bound the route cache; negative caching (done 2026-07-10)

> Cache capped at 1000 entries (oldest 10% evicted on overflow); empty
> results cached with a 5-minute negative TTL so unroutable pairs don't
> re-run Yen's.  Bonus fix: `find_routes` now returns `[]` for
> disconnected stops instead of leaking `NetworkXNoPath` (which the API
> surfaced as a 500 — now a proper 404).

### ✅ Move `/ingest/gtfs-static` to a background task (done 2026-07-10)

> Returns `202` immediately and runs `_run_gtfs_ingest` as an asyncio task
> on its own session; `GET /ingest/status` (same optional API key) reports
> running/last_status/last_message.  Manual ingest and the daily refresh
> share a single slot — concurrent attempts get `409` / the daily job
> skips its cycle.

### ✅ Surface GTFS-RT feed freshness in `/health` (done 2026-07-10)

> `get_rt_status()` merged into the `gtfs_rt` health section:
> `last_fetched_at`, `consecutive_failures`, `backing_off_until`,
> `polling_coverage_since`, and live entity counts.

### ✅ `GET /alerts` endpoint (done 2026-07-10)

> Read-only, rate-limited endpoint over the in-memory `service_alerts`
> snapshot (header, description, affected routes/stops, fetched_at).

### ✅ Security niceties (done 2026-07-10)

> `_require_ingest_key` uses `secrets.compare_digest`; Gemini key moved
> from the `?key=` URL param to the `x-goog-api-key` header.

## Developer experience

### ✅ CI (done 2026-07-10)

> `.github/workflows/ci.yml`: lint + unit tests (uv, SQLite) on every
> push/PR, plus an integration job running `tests/integration/` against a
> `postgis/postgis` service container with the schema created via
> `init_db()`.

### ✅ Linter (done 2026-07-10)

> ruff configured in `pyproject.toml` (E4/E7/E9, F, I; line-length 100,
> E501 not selected) and wired into CI; codebase passes clean.  mypy
> remains optional future work.

### ✅ `requirements.txt` deleted (done 2026-07-10)

> It contradicted `pyproject.toml` (listed unused `anthropic`, omitted
> `psycopg`/`geoalchemy2`/`shapely`).  uv + `uv.lock` is the source of
> truth; regenerate with `uv export` if ever needed.

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
-- 2026-07-10 (eighth pass): counters became Float so the daily decay
-- actually decays small values (integer ROUND froze everything <= 10).
-- SQLite needs nothing (dynamic typing); PostgreSQL needs:
ALTER TABLE reliability_records
  ALTER COLUMN scheduled_departures TYPE DOUBLE PRECISION,
  ALTER COLUMN observed_departures  TYPE DOUBLE PRECISION,
  ALTER COLUMN total_delay_seconds  TYPE DOUBLE PRECISION,
  ALTER COLUMN cancellation_count   TYPE DOUBLE PRECISION;
```

(`observed_trips` is a new table — `init_db()`/`create_all` adds it
automatically.)  If the project ever needs regular schema changes, consider
adopting Alembic instead of manual SQL.

## Docs

### ✅ Config docs drift (done 2026-07-10)

> `.env.example` and the README config table now cover every `config.py`
> variable (incl. new `AGENCY_TZ` and `RATE_LIMIT_PER_MINUTE`); README API
> docs updated for 202 ingest + `/ingest/status`, `/alerts`, 429s,
> expected times, and the no-show / decay risk-model changes.

## Performance (later)

### ✅ Ingest memory footprint (done 2026-07-10)

> `_parse_stop_times` iterates `df.itertuples` and saves in 50k-row chunks
> instead of materialising ~2M dicts + ORM objects at once.

### ✅ Batch historical reliability lookups (done 2026-07-10)

> `get_historical_reliability_batch` fetches every (route, stop, bucket)
> triple for the request in one tuple-`IN` query; `_score_routes_blocking`
> falls back to `NEUTRAL_PRIOR` for missing triples.
