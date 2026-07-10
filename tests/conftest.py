"""
Pin the unit-test environment before any application module is imported.

config.py reads .env at import time via load_dotenv(), so a developer's
local .env (e.g. one pointing DATABASE_URL at the Docker PostgreSQL) would
otherwise leak into the unit suite — the PostGIS Geography column on Stop
cannot be created on the SQLite databases these tests build.

This conftest runs before config.py is imported and before load_dotenv()
executes; load_dotenv never overrides variables that are already set, so
the setdefault below beats .env while still respecting an explicit shell
export (which is how tests/integration/ opts in to PostgreSQL:
DATABASE_URL=postgresql+... uv run pytest tests/integration/ -q).
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# The unit suite fires hundreds of requests from one "client IP" — disable
# the per-IP rate limit.  The rate-limit tests patch the constant back on.
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "0")

# A developer .env with a real Metrolinx key would otherwise make every
# TestClient startup poll the live GTFS-RT feeds (real network I/O, and
# live alerts/trip updates leak into module-level state, failing tests
# that expect empty snapshots).  Unit tests must never touch the network.
os.environ["GTFS_RT_API_KEY"] = ""
