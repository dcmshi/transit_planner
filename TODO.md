# TODO — audit backlog

Updated 2026-07-10, consolidated after the seventh and eighth audit passes
(two independent reviewers each ran an eighth-pass audit; their findings
overlapped and every item is fixed or explicitly deferred below).  Full
findings, fix notes, and live-verification records are in `PROGRESS.md`;
per-item detail lives in the commit messages for 2026-07-10.

## Open items

### ✅ Split `api/main.py` into modules (done 2026-07-10)

> `api/main.py` is now ~35 lines of app assembly; concerns moved to
> `api/routes.py` (endpoints + scoring pipeline), `api/lifespan.py`
> (startup/shutdown, scheduler jobs, ingest slot), `api/cache.py`, and
> `api/ratelimit.py`.  All test patch targets updated to the real new
> module paths (no re-export shims — they would silently break `patch()`
> semantics).  `uvicorn api.main:app` entry point unchanged.

### Schema migration for other existing deployments

This machine's Docker DB is fully migrated in place (2026-06-10 and
2026-07-10).  Any *other* pre-existing PostgreSQL volume needs the
following (or a `docker compose down -v` reset):

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

## Deferred / open by design

- **`total_travel_seconds` excludes leading/trailing walk legs** from the
  door-to-door duration (`routing/engine.py`) — a semantics change;
  revisit together with the frontend's display of durations.
- **LLM explanation cache** — identical journeys re-run Ollama/Gemini per
  request; a small TTL cache keyed on the scored-route signature would do.
- **Rate limiting behind a proxy** — keys on `request.client.host`, so a
  reverse proxy would collapse all callers into one bucket.  Fine for the
  documented single-worker local deployment; use X-Forwarded-For from a
  trusted proxy if ever deployed behind one.
- **Decay assumes the daily job runs daily** — `days_elapsed` is fixed at
  1.0, so days the refresh doesn't run simply don't decay (slightly
  stretches the effective window).  Fold into any future decay rework.
- **`calendar.txt` / `exception_type=1` still unused by routing** — the
  service_id-is-a-date convention is now validated at ingest (aborts
  loudly on a convention change), but full ServiceCalendar-based service
  resolution only becomes necessary if Metrolinx actually changes format.
- **No-show sweep skips >24:00:00 final departures** — their service day
  ends before the cutoff can pass.  Documented; irrelevant for the
  Toronto–Guelph corridor where service ends before midnight.
- **Risk aggregation: max leg risk vs weighted sum** (ADR-006) — revisit
  once enough real GTFS-RT observations accumulate.
- **mypy** — optional; the codebase is well-annotated so it's mostly free
  coverage on top of ruff.
