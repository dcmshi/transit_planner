"""
Integration tests for the Alembic migration chain.

PostgreSQL only, and not merely for convenience: Stop.geog is a GeoAlchemy2
Geography column when DATABASE_URL points at PostgreSQL, so the baseline
migration emits geospatial DDL that plain SQLite cannot execute.  SQLite
builds its schema with init_db()/create_all instead and never runs these.

Each test builds a throwaway database so migrating cannot touch real data.

Run with:
    DATABASE_URL=postgresql+psycopg://transit:transit@localhost:5432/transit \
    uv run pytest tests/integration/ -q
"""

import os

import pytest
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in DATABASE_URL,
    reason="requires PostgreSQL + PostGIS (set DATABASE_URL env var)",
)

_SCRATCH_DB = "alembic_test_scratch"


@pytest.fixture
def scratch_db_url():
    """An empty PostGIS database, dropped afterwards."""
    admin = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {_SCRATCH_DB}"))
        conn.execute(text(f"CREATE DATABASE {_SCRATCH_DB}"))

    url = DATABASE_URL.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"
    scratch = create_engine(url, isolation_level="AUTOCOMMIT")
    with scratch.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    scratch.dispose()

    yield url

    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {_SCRATCH_DB}"))
    admin.dispose()


def _run(url, target):
    """Run alembic against `url` in a subprocess.

    env.py reads config.DATABASE_URL at import time, and config.py reads the
    environment at import time, so the URL has to be set before the process
    starts — patching in-process would be ignored.
    """
    import subprocess
    import sys

    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade" if target != "base" else "downgrade", target],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"
    return result


class TestMigrations:

    def test_upgrade_head_builds_the_schema(self, scratch_db_url):
        _run(scratch_db_url, "head")

        engine = create_engine(scratch_db_url)
        with engine.connect() as conn:
            tables = {
                r[0] for r in conn.execute(text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                ))
            }
        engine.dispose()

        assert {
            "stops", "routes", "trips", "stop_times", "service_calendar",
            "service_calendar_dates", "observed_trips", "reliability_records",
        } <= tables

    def test_migrated_schema_matches_the_models(self, scratch_db_url):
        """The assertion that keeps migrations honest: after upgrading, an
        autogenerate pass must find nothing left to do."""
        _run(scratch_db_url, "head")

        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from geoalchemy2 import alembic_helpers

        from db.models import Base

        engine = create_engine(scratch_db_url)
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn, opts={"include_object": alembic_helpers.include_object}
            )
            diff = compare_metadata(ctx, Base.metadata)
        engine.dispose()

        assert diff == [], f"migrations drifted from db/models.py: {diff}"

    def test_downgrade_removes_everything(self, scratch_db_url):
        _run(scratch_db_url, "head")
        _run(scratch_db_url, "base")

        engine = create_engine(scratch_db_url)
        with engine.connect() as conn:
            remaining = conn.execute(text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename NOT IN ('spatial_ref_sys', 'alembic_version')"
            )).scalar()
        engine.dispose()

        assert remaining == 0

    def test_geography_column_survives_the_round_trip(self, scratch_db_url):
        """geog is the column autogenerate is most likely to get wrong, and
        the GIST index is created by GeoAlchemy2 rather than by the model."""
        _run(scratch_db_url, "head")

        engine = create_engine(scratch_db_url)
        with engine.connect() as conn:
            udt = conn.execute(text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_name = 'stops' AND column_name = 'geog'"
            )).scalar()
            has_gist = conn.execute(text(
                "SELECT count(*) FROM pg_indexes "
                "WHERE tablename = 'stops' AND indexname = 'idx_stops_geog'"
            )).scalar()
        engine.dispose()

        assert udt == "geography"
        assert has_gist == 1
