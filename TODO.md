# TODO — audit backlog

Updated 2026-07-10, consolidated after the seventh and eighth audit passes
(two independent reviewers each ran an eighth-pass audit; their findings
overlapped and every item is fixed or explicitly deferred below).  Full
findings, fix notes, and live-verification records are in `PROGRESS.md`;
per-item detail lives in the commit messages for 2026-07-10.

## Open items

### ✅ mypy in CI (done 2026-07-31)

> `uv run mypy` is clean across all 38 source files and gates CI as a `Type
> check` step in the `lint-and-test` job.  Config in `pyproject.toml`:
> `disallow_untyped_defs` for application code, relaxed for `tests.*` (the
> only violations were 434 pytest functions missing `-> None`), plus
> `ignore_missing_imports` for `google.transit` and `apscheduler`, which
> publish no stubs.  Stub packages added as dev deps: `pandas-stubs`,
> `types-networkx`, `types-shapely`.
>
> The bulk of the 60 initial errors traced to `Base = declarative_base()`,
> which mypy cannot type at all — `db/models.py` now uses SQLAlchemy 2.0
> `DeclarativeBase` with `Mapped[...]`/`mapped_column()`, verified DDL-neutral
> by diffing `CreateTable`/`CreateIndex` output plus per-column nullable,
> default, PK, and FK facts on both the SQLite and PostGIS branches.
>
> Two genuine defects fell out: nullable reliability counters raised
> `TypeError` on any NULL row (`None < _MIN_SCHEDULED`) — now routed to the
> neutral prior via `_count()`, with regression tests — and a `legs` loop
> variable in `routing/engine.py` shadowed an `Optional` result, making the
> `if legs is None` guard dead to the type checker.

### ✅ Split `api/main.py` into modules (done 2026-07-10)

> `api/main.py` is now ~35 lines of app assembly; concerns moved to
> `api/routes.py` (endpoints + scoring pipeline), `api/lifespan.py`
> (startup/shutdown, scheduler jobs, ingest slot), `api/cache.py`, and
> `api/ratelimit.py`.  All test patch targets updated to the real new
> module paths (no re-export shims — they would silently break `patch()`
> semantics).  `uvicorn api.main:app` entry point unchanged.

### Corridor assumptions block arbitrary origin/destination pairs

The routing itself is already origin/destination agnostic: `/routes` takes
any two `stop_id`s and only rejects `origin == destination`, ingestion pulls
the entire Metrolinx GO feed with no route filter (44 routes / 889 stops in
the Docker DB), and nothing in `routing/engine.py` restricts the pair.  What
*is* corridor-specific is a set of past-midnight assumptions justified by
"GO Transit Toronto–Guelph service ends well before midnight" — and that
premise is false for the feed already loaded: **81,531 stop_times across
5,995 trips have `departure_time >= 24:00:00`.**

Work needed to honestly support the whole network:

- `routing/engine.py` `_fill_later_departures` caps `not_before` at
  `MAX_SECONDS = 23:59:59`, so any path whose next departure is a >= 24:00:00
  GTFS time is dropped instead of returned.
- The no-show sweep in `ingestion/gtfs_realtime.py` skips >= 24:00:00 final
  departures (currently listed below as deferred on the same false premise) —
  those trips never get their `scheduled_departures` incremented, so
  reliability silently under-counts the late-evening network.
- `calendar.txt` / `exception_type` service resolution (also deferred below)
  matters more once trips outside the corridor are in scope, since the
  service_id-is-a-date convention was only validated against it.
- Docs and the FastAPI `description` in `api/main.py` still say
  "Toronto ↔ Guelph".

### `test_matches_bisect_result` only passes on an empty stops table

`tests/integration/test_walk_edges_postgis.py::TestWalkEdgesPostGIS::test_matches_bisect_result`
inserts three `_TEST_*` stops, then compares `_add_walk_edges_postgis` against
`_add_walk_edges_bisect`.  The two helpers take their stop set from different
places, and the test only controls one of them:

- `_add_walk_edges_bisect(G, stops)` iterates exactly the list handed to it —
  the three `MagicMock` fixtures — and yields 4 edges.
- `_add_walk_edges_postgis(G, session)` ignores `G`'s existing nodes and runs
  an unfiltered `stops`-against-`stops` self-join, so it returns every
  within-500 m pair in the database.  Because `G.add_edge()` creates endpoints
  implicitly, the call grows the graph from the 3 seeded nodes to **823**, and
  the assertion compares **2040 edges against 4**.

**The behaviour under test is not broken.**  Feeding both implementations the
same 889 ingested stops produces 2028 edges each with zero set difference, and
restricted to the three fixtures they return the identical 4 edges
(`_TEST_E`↔`_TEST_F`, `_TEST_F`↔`_TEST_G`).  Only the test's isolation is wrong.

CI stays green because its Postgres service starts empty and runs only
`init_db()`, so the self-join has nothing else to find.  Fix by scoping the
PostGIS query to the graph's nodes — which would also align the function's
contract with the bisect variant, whose behaviour production already relies on
`_add_stop_nodes()` having run first.

Pre-existing — confirmed failing on `9935429`, before the mypy work.

(Aside: the `_TEST_G` fixture is commented "~520 m — just outside 500 m limit",
which is true of its distance to `_TEST_E` but not to `_TEST_F` at ~467 m, so
an `F`↔`G` edge is expected.  Both implementations agree on it; the comment is
just narrower than it reads.)

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
  ends before the cutoff can pass.  Was deferred as "irrelevant for the
  Toronto–Guelph corridor where service ends before midnight"; that premise
  does not hold for the feed actually ingested (5,995 trips have
  >= 24:00:00 departures), so this is now tracked under the corridor item
  above rather than deferred on those grounds.
- **Risk aggregation: max leg risk vs weighted sum** (ADR-006) — revisit
  once enough real GTFS-RT observations accumulate.
