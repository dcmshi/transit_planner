from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config import DATABASE_URL
from db.models import Base

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    # Sized for the threaded access pattern: /routes scoring runs in
    # asyncio.to_thread workers, sync endpoints on the FastAPI threadpool,
    # plus scheduler jobs — the default pool (5 + 10 overflow) can time out
    # under moderate concurrency.  pool_pre_ping recovers from DB restarts
    # instead of erroring on stale pooled connections.
    engine = create_engine(
        DATABASE_URL,
        pool_size=15,
        max_overflow=25,
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
    # GeoAlchemy2 automatically creates a GIST index (idx_stops_geog) on
    # stops.geog when the Geography column is created — no manual index needed.


def get_session() -> Iterator[Session]:
    """Dependency-injectable session factory for FastAPI routes."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
