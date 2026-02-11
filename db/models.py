"""
SQLAlchemy ORM models for GTFS static data and reliability tracking.

GTFS time fields (arrival_time, departure_time) are stored as HH:MM:SS strings
because the GTFS spec allows values >= 24:00:00 for trips crossing midnight.
Application code converts to integer seconds-past-midnight when needed.
"""

from sqlalchemy import (
    Boolean, Column, Float, ForeignKey, Integer, String
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Stop(Base):
    __tablename__ = "stops"

    stop_id = Column(String, primary_key=True)
    stop_name = Column(String, nullable=False)
    stop_lat = Column(Float, nullable=False)
    stop_lon = Column(Float, nullable=False)
    stop_code = Column(String, nullable=True)

    stop_times = relationship("StopTime", back_populates="stop")


class Route(Base):
    __tablename__ = "routes"

    route_id = Column(String, primary_key=True)
    route_short_name = Column(String)
    route_long_name = Column(String)
    route_type = Column(Integer)  # 3 = bus

    trips = relationship("Trip", back_populates="route")


class Trip(Base):
    __tablename__ = "trips"

    trip_id = Column(String, primary_key=True)
    route_id = Column(String, ForeignKey("routes.route_id"), index=True)
    service_id = Column(String, index=True)
    trip_headsign = Column(String)
    direction_id = Column(Integer)
    shape_id = Column(String, nullable=True)

    route = relationship("Route", back_populates="trips")
    stop_times = relationship("StopTime", back_populates="trip", order_by="StopTime.stop_sequence")


class StopTime(Base):
    __tablename__ = "stop_times"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(String, ForeignKey("trips.trip_id"), index=True)
    arrival_time = Column(String)    # HH:MM:SS (may exceed 24:00:00)
    departure_time = Column(String)  # HH:MM:SS (may exceed 24:00:00)
    stop_id = Column(String, ForeignKey("stops.stop_id"), index=True)
    stop_sequence = Column(Integer)

    trip = relationship("Trip", back_populates="stop_times")
    stop = relationship("Stop", back_populates="stop_times")


class ServiceCalendar(Base):
    __tablename__ = "service_calendar"

    service_id = Column(String, primary_key=True)
    monday = Column(Boolean)
    tuesday = Column(Boolean)
    wednesday = Column(Boolean)
    thursday = Column(Boolean)
    friday = Column(Boolean)
    saturday = Column(Boolean)
    sunday = Column(Boolean)
    start_date = Column(String)  # YYYYMMDD
    end_date = Column(String)    # YYYYMMDD


class ServiceCalendarDate(Base):
    __tablename__ = "service_calendar_dates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_id = Column(String, index=True)
    date = Column(String, index=True)  # YYYYMMDD
    exception_type = Column(Integer)   # 1 = service added, 2 = service removed


class ReliabilityRecord(Base):
    """Rolling-window reliability stats per route / stop / time bucket."""
    __tablename__ = "reliability_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    route_id = Column(String, index=True)
    stop_id = Column(String, index=True)
    # e.g. "weekday_am_peak", "weekday_pm_peak", "weekday_offpeak", "weekend"
    time_bucket = Column(String, index=True)
    observed_departures = Column(Integer, default=0)
    scheduled_departures = Column(Integer, default=0)
    total_delay_seconds = Column(Integer, default=0)
    cancellation_count = Column(Integer, default=0)
    window_start_date = Column(String)  # YYYYMMDD
    window_end_date = Column(String)    # YYYYMMDD
    updated_at = Column(String)         # ISO 8601 timestamp
