from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from config import DATABASE_URL
from db.models import Base

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
    # GeoAlchemy2 automatically creates a GIST index (idx_stops_geog) on
    # stops.geog when the Geography column is created — no manual index needed.


def get_session() -> Session:
    """Dependency-injectable session factory for FastAPI routes."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
