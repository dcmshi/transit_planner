# TODO — deferred / nice-to-have

Items consciously deferred from the sixth audit pass + live e2e verification
(2026-06-10).  None are blocking; see `PROGRESS.md` for everything completed.

## Rate limiting on public endpoints

No rate limiting exists on `/routes` or `/stops`.  The route cache absorbs
repeated identical queries, but unique route queries each cost real CPU
(Yen's + scheduling), so a hostile client could exhaust the worker pool.
Only matters if the API faces the public internet.

- Suggested: [slowapi](https://github.com/laurentS/slowapi) middleware,
  per-IP, e.g. 100 req/min on `/routes`, looser on `/health`.
- `/ingest/*` is already gated by `INGEST_API_KEY` when set.

## Split `api/main.py` into modules

~600 lines mixing lifespan/scheduler setup, route-cache helpers (incl.
single-flight locks), and all endpoint handlers.  Readable as-is — purely
cosmetic.  Natural split if it grows further:

- `api/cache.py` — `_routes_cache`, TTL, single-flight locks
- `api/lifespan.py` — startup/shutdown, scheduled jobs
- `api/routes.py` — endpoint handlers (incl. `_score_routes_blocking`)

## Schema migration for other existing deployments

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
