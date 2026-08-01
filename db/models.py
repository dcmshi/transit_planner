"""
SQLAlchemy ORM models for GTFS static data and reliability tracking.

GTFS time fields (arrival_time, departure_time) are stored as HH:MM:SS strings
because the GTFS spec allows values >= 24:00:00 for trips crossing midnight.
Application code converts to integer seconds-past-midnight when needed.

Column types come from the `Mapped[...]` annotations: `Mapped[str]` is a
NOT NULL VARCHAR, `Mapped[str | None]` a nullable one.  Annotate nullability
deliberately — it is the DDL, not a hint.
"""

from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import DATABASE_URL

try:
    from geoalchemy2 import Geography as _Geography
    _HAS_POSTGIS = DATABASE_URL.startswith("postgresql")
except ImportError:
    _HAS_POSTGIS = False


class Base(DeclarativeBase):
    pass


class Stop(Base):
    __tablename__ = "stops"

    stop_id: Mapped[str] = mapped_column(primary_key=True)
    stop_name: Mapped[str]
    stop_lat: Mapped[float]
    stop_lon: Mapped[float]
    stop_code: Mapped[str | None]
    # PostGIS geography column — populated during ingestion when using PostgreSQL.
    # On SQLite (tests/dev) the column is a plain String and is not used.
    geog: Mapped[Any | None] = mapped_column(
        _Geography(geometry_type="POINT", srid=4326) if _HAS_POSTGIS else String,
        nullable=True,
    )

    stop_times: Mapped[list["StopTime"]] = relationship(back_populates="stop")


class Route(Base):
    __tablename__ = "routes"

    route_id: Mapped[str] = mapped_column(primary_key=True)
    route_short_name: Mapped[str | None]
    route_long_name: Mapped[str | None]
    route_type: Mapped[int | None]  # 3 = bus

    trips: Mapped[list["Trip"]] = relationship(back_populates="route")


class Trip(Base):
    __tablename__ = "trips"

    trip_id: Mapped[str] = mapped_column(primary_key=True)
    route_id: Mapped[str | None] = mapped_column(ForeignKey("routes.route_id"), index=True)
    service_id: Mapped[str | None] = mapped_column(index=True)
    trip_headsign: Mapped[str | None]
    direction_id: Mapped[int | None]
    shape_id: Mapped[str | None]

    route: Mapped["Route | None"] = relationship(back_populates="trips")
    stop_times: Mapped[list["StopTime"]] = relationship(
        back_populates="trip", order_by="StopTime.stop_sequence"
    )


class StopTime(Base):
    __tablename__ = "stop_times"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trip_id: Mapped[str | None] = mapped_column(ForeignKey("trips.trip_id"), index=True)
    arrival_time: Mapped[str]    # HH:MM:SS (may exceed 24:00:00)
    departure_time: Mapped[str]  # HH:MM:SS (may exceed 24:00:00)
    stop_id: Mapped[str | None] = mapped_column(ForeignKey("stops.stop_id"), index=True)
    stop_sequence: Mapped[int | None]

    trip: Mapped["Trip | None"] = relationship(back_populates="stop_times")
    stop: Mapped["Stop | None"] = relationship(back_populates="stop_times")


class Shape(Base):
    """
    One GTFS shape as an ordered polyline.

    Stored as a single JSON `[[lon, lat], ...]` row rather than the feed's
    one-row-per-point form: every read wants the whole polyline, and the GO
    feed's 502,060 points collapse to 314 rows this way.
    """
    __tablename__ = "shapes"

    shape_id: Mapped[str] = mapped_column(primary_key=True)
    points: Mapped[str]  # JSON [[lon, lat], ...] in shape_pt_sequence order


class ShapeStopPosition(Base):
    """
    Where a stop falls along a shape, as an index into Shape.points.

    This is the `shape_dist_traveled` the GO feed does not publish — neither
    shapes.txt nor stop_times.txt carries it, so each stop is projected onto
    the polyline at ingest.  Doing it here keeps request-time slicing to a
    list slice instead of a projection per leg.
    """
    __tablename__ = "shape_stop_positions"

    shape_id: Mapped[str] = mapped_column(ForeignKey("shapes.shape_id"), primary_key=True)
    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.stop_id"), primary_key=True)
    point_index: Mapped[int]


class StopRoute(Base):
    """
    Which routes call at which stop — derived, not from the feed directly.

    /stops answers this by joining stop_times to trips and taking DISTINCT,
    which reads ~72,000 stop_times rows to produce a few dozen pairs.  The
    answer changes only when the schedule does, so it is materialised here at
    ingest instead of recomputed per request.
    """
    __tablename__ = "stop_routes"

    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.stop_id"), primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.route_id"), primary_key=True)


class ServiceCalendar(Base):
    __tablename__ = "service_calendar"

    service_id: Mapped[str] = mapped_column(primary_key=True)
    monday: Mapped[bool | None]
    tuesday: Mapped[bool | None]
    wednesday: Mapped[bool | None]
    thursday: Mapped[bool | None]
    friday: Mapped[bool | None]
    saturday: Mapped[bool | None]
    sunday: Mapped[bool | None]
    start_date: Mapped[str]  # YYYYMMDD
    end_date: Mapped[str]    # YYYYMMDD


class ServiceCalendarDate(Base):
    __tablename__ = "service_calendar_dates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    service_id: Mapped[str | None] = mapped_column(index=True)
    date: Mapped[str | None] = mapped_column(index=True)  # YYYYMMDD
    exception_type: Mapped[int | None]  # 1 = service added, 2 = service removed


class ObservedTrip(Base):
    """
    Dedup marker: trips whose RT observations were recorded on a given day.

    Persists the observe_departures() in-memory dedup set so a process
    restart mid-day cannot double-count a trip.  Rows from previous days
    are purged on date rollover.
    """
    __tablename__ = "observed_trips"

    trip_id: Mapped[str] = mapped_column(primary_key=True)
    recorded_date: Mapped[str] = mapped_column(primary_key=True)  # YYYYMMDD (agency-local)


class ReliabilityRecord(Base):
    """Rolling-window reliability stats per route / stop / time bucket."""
    __tablename__ = "reliability_records"
    # All lookups filter on the full (route_id, stop_id, time_bucket) triple;
    # one composite index serves them (and route_id-prefix queries) better
    # than three single-column indexes.
    __table_args__ = (
        Index("ix_reliability_route_stop_bucket", "route_id", "stop_id", "time_bucket"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    route_id: Mapped[str | None]
    stop_id: Mapped[str | None]
    # e.g. "weekday_am_peak", "weekday_pm_peak", "weekday_offpeak", "weekend"
    time_bucket: Mapped[str | None]
    # Provenance of the counts: "seed" = synthetic prior from the static
    # schedule, "observed" = built from real GTFS-RT observations only,
    # "mixed" = seeded record that has since absorbed real observations.
    source: Mapped[str] = mapped_column(default="observed")
    # Float, not Integer: the daily exponential decay multiplies these by
    # ~0.95 — integer rounding made every value <= 10 a fixed point that
    # never decayed, permanently freezing sparse (often bad) records.
    observed_departures: Mapped[float | None] = mapped_column(default=0)
    scheduled_departures: Mapped[float | None] = mapped_column(default=0)
    total_delay_seconds: Mapped[float | None] = mapped_column(default=0)
    cancellation_count: Mapped[float | None] = mapped_column(default=0)
    window_start_date: Mapped[str | None]  # YYYYMMDD
    window_end_date: Mapped[str | None]    # YYYYMMDD
    updated_at: Mapped[str | None]         # ISO 8601 timestamp
