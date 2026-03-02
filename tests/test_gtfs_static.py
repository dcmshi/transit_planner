"""
Unit tests for ingestion/gtfs_static.py.

All tests use an in-memory SQLite database (StaticPool) so no Docker or
external DB is required.  HTTP calls in download_gtfs_zip() are mocked.
parse_and_store() is tested via a minimal in-memory zip.
"""

import io
import zipfile

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import AsyncMock, MagicMock, patch

from db.models import (
    Base,
    Route,
    ServiceCalendar,
    ServiceCalendarDate,
    Stop,
    StopTime,
    Trip,
)
from ingestion.gtfs_static import (
    _parse_calendar,
    _parse_calendar_dates,
    _parse_routes,
    _parse_stop_times,
    _parse_stops,
    _parse_trips,
    parse_and_store,
)


# ---------------------------------------------------------------------------
# Shared in-memory DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# _parse_stops
# ---------------------------------------------------------------------------

class TestParseStops:
    def test_basic_insert(self, db):
        df = pd.DataFrame([
            {"stop_id": "S1", "stop_name": "Stop One", "stop_lat": "43.0", "stop_lon": "-79.0"},
            {"stop_id": "S2", "stop_name": "Stop Two", "stop_lat": "43.1", "stop_lon": "-79.1"},
        ])
        _parse_stops(df, db)
        db.flush()
        stops = db.query(Stop).order_by(Stop.stop_id).all()
        assert len(stops) == 2
        assert stops[0].stop_id == "S1"
        assert stops[0].stop_name == "Stop One"
        assert stops[0].stop_lat == pytest.approx(43.0)
        assert stops[0].stop_lon == pytest.approx(-79.0)

    def test_clears_existing_stops(self, db):
        db.add(Stop(stop_id="OLD", stop_name="Old Stop", stop_lat=0.0, stop_lon=0.0))
        db.flush()
        df = pd.DataFrame([
            {"stop_id": "NEW", "stop_name": "New Stop", "stop_lat": "1.0", "stop_lon": "1.0"},
        ])
        _parse_stops(df, db)
        db.flush()
        stop_ids = [s.stop_id for s in db.query(Stop).all()]
        assert "OLD" not in stop_ids
        assert "NEW" in stop_ids

    def test_optional_stop_code(self, db):
        df = pd.DataFrame([
            {"stop_id": "S1", "stop_name": "Stop", "stop_lat": "43.0", "stop_lon": "-79.0",
             "stop_code": "SC1"},
        ])
        _parse_stops(df, db)
        db.flush()
        stop = db.query(Stop).filter_by(stop_id="S1").first()
        assert stop.stop_code == "SC1"

    def test_empty_dataframe(self, db):
        df = pd.DataFrame(columns=["stop_id", "stop_name", "stop_lat", "stop_lon"])
        _parse_stops(df, db)
        db.flush()
        assert db.query(Stop).count() == 0


# ---------------------------------------------------------------------------
# _parse_routes
# ---------------------------------------------------------------------------

class TestParseRoutes:
    def test_basic_insert(self, db):
        df = pd.DataFrame([
            {"route_id": "R1", "route_short_name": "1", "route_long_name": "Route One",
             "route_type": "3"},
            {"route_id": "R2", "route_short_name": "2", "route_long_name": "Route Two",
             "route_type": "3"},
        ])
        _parse_routes(df, db)
        db.flush()
        routes = db.query(Route).order_by(Route.route_id).all()
        assert len(routes) == 2
        assert routes[0].route_id == "R1"
        assert routes[0].route_short_name == "1"
        assert routes[0].route_type == 3

    def test_clears_existing_routes(self, db):
        db.add(Route(route_id="OLD", route_short_name="X", route_long_name="Old", route_type=3))
        db.flush()
        df = pd.DataFrame([
            {"route_id": "NEW", "route_short_name": "Y", "route_long_name": "New",
             "route_type": "3"},
        ])
        _parse_routes(df, db)
        db.flush()
        route_ids = [r.route_id for r in db.query(Route).all()]
        assert "OLD" not in route_ids
        assert "NEW" in route_ids

    def test_default_route_type(self, db):
        df = pd.DataFrame([
            {"route_id": "R1", "route_short_name": "1", "route_long_name": "One",
             "route_type": ""},
        ])
        _parse_routes(df, db)
        db.flush()
        route = db.query(Route).filter_by(route_id="R1").first()
        assert route.route_type == 3  # default


# ---------------------------------------------------------------------------
# _parse_trips — FK filtering
# ---------------------------------------------------------------------------

class TestParseTrips:
    def _seed_route(self, db):
        db.add(Route(route_id="R1", route_short_name="1", route_long_name="", route_type=3))
        db.flush()

    def test_valid_trip_inserted(self, db):
        self._seed_route(db)
        df = pd.DataFrame([
            {"trip_id": "T1", "route_id": "R1", "service_id": "SVC1",
             "trip_headsign": "Guelph", "direction_id": "0", "shape_id": ""},
        ])
        _parse_trips(df, db)
        db.flush()
        assert db.query(Trip).filter_by(trip_id="T1").first() is not None

    def test_orphaned_trip_skipped(self, db):
        self._seed_route(db)
        df = pd.DataFrame([
            {"trip_id": "T_bad", "route_id": "MISSING_ROUTE", "service_id": "SVC1",
             "trip_headsign": "", "direction_id": "0", "shape_id": ""},
        ])
        _parse_trips(df, db)
        db.flush()
        assert db.query(Trip).filter_by(trip_id="T_bad").first() is None

    def test_mixed_valid_and_orphaned(self, db):
        self._seed_route(db)
        df = pd.DataFrame([
            {"trip_id": "T_good", "route_id": "R1", "service_id": "SVC1",
             "trip_headsign": "", "direction_id": "0", "shape_id": ""},
            {"trip_id": "T_bad", "route_id": "NO_SUCH_ROUTE", "service_id": "SVC1",
             "trip_headsign": "", "direction_id": "0", "shape_id": ""},
        ])
        _parse_trips(df, db)
        db.flush()
        trip_ids = [t.trip_id for t in db.query(Trip).all()]
        assert "T_good" in trip_ids
        assert "T_bad" not in trip_ids

    def test_clears_existing_trips(self, db):
        self._seed_route(db)
        db.add(Trip(trip_id="OLD", route_id="R1", service_id="SVC", trip_headsign="", direction_id=0))
        db.flush()
        df = pd.DataFrame([
            {"trip_id": "NEW", "route_id": "R1", "service_id": "SVC",
             "trip_headsign": "", "direction_id": "0", "shape_id": ""},
        ])
        _parse_trips(df, db)
        db.flush()
        trip_ids = [t.trip_id for t in db.query(Trip).all()]
        assert "OLD" not in trip_ids
        assert "NEW" in trip_ids

    def test_missing_direction_id_defaults_to_zero(self, db):
        self._seed_route(db)
        df = pd.DataFrame([
            {"trip_id": "T1", "route_id": "R1", "service_id": "SVC1",
             "trip_headsign": "", "direction_id": "", "shape_id": ""},
        ])
        _parse_trips(df, db)
        db.flush()
        trip = db.query(Trip).filter_by(trip_id="T1").first()
        assert trip.direction_id == 0


# ---------------------------------------------------------------------------
# _parse_stop_times — FK filtering
# ---------------------------------------------------------------------------

class TestParseStopTimes:
    def _seed(self, db):
        db.add(Stop(stop_id="S1", stop_name="Stop 1", stop_lat=43.0, stop_lon=-79.0))
        db.add(Stop(stop_id="S2", stop_name="Stop 2", stop_lat=43.1, stop_lon=-79.1))
        db.add(Route(route_id="R1", route_short_name="1", route_long_name="", route_type=3))
        db.add(Trip(trip_id="T1", route_id="R1", service_id="SVC", trip_headsign="", direction_id=0))
        db.flush()

    def test_valid_stop_times_inserted(self, db):
        self._seed(db)
        df = pd.DataFrame([
            {"trip_id": "T1", "stop_id": "S1", "arrival_time": "08:00:00",
             "departure_time": "08:00:00", "stop_sequence": "1"},
            {"trip_id": "T1", "stop_id": "S2", "arrival_time": "08:30:00",
             "departure_time": "08:30:00", "stop_sequence": "2"},
        ])
        _parse_stop_times(df, db)
        db.flush()
        assert db.query(StopTime).count() == 2

    def test_orphaned_trip_id_skipped(self, db):
        self._seed(db)
        df = pd.DataFrame([
            {"trip_id": "T_MISSING", "stop_id": "S1", "arrival_time": "08:00:00",
             "departure_time": "08:00:00", "stop_sequence": "1"},
        ])
        _parse_stop_times(df, db)
        db.flush()
        assert db.query(StopTime).count() == 0

    def test_orphaned_stop_id_skipped(self, db):
        self._seed(db)
        df = pd.DataFrame([
            {"trip_id": "T1", "stop_id": "S_MISSING", "arrival_time": "08:00:00",
             "departure_time": "08:00:00", "stop_sequence": "1"},
        ])
        _parse_stop_times(df, db)
        db.flush()
        assert db.query(StopTime).count() == 0

    def test_mixed_valid_and_orphaned(self, db):
        self._seed(db)
        df = pd.DataFrame([
            {"trip_id": "T1", "stop_id": "S1", "arrival_time": "08:00:00",
             "departure_time": "08:00:00", "stop_sequence": "1"},
            {"trip_id": "T1", "stop_id": "S_GHOST", "arrival_time": "08:15:00",
             "departure_time": "08:15:00", "stop_sequence": "2"},
        ])
        _parse_stop_times(df, db)
        db.flush()
        assert db.query(StopTime).count() == 1
        assert db.query(StopTime).filter_by(stop_id="S1").first() is not None

    def test_clears_existing_stop_times(self, db):
        self._seed(db)
        db.add(StopTime(trip_id="T1", stop_id="S1", arrival_time="07:00:00",
                        departure_time="07:00:00", stop_sequence=99))
        db.flush()
        df = pd.DataFrame([
            {"trip_id": "T1", "stop_id": "S1", "arrival_time": "08:00:00",
             "departure_time": "08:00:00", "stop_sequence": "1"},
        ])
        _parse_stop_times(df, db)
        db.flush()
        times = db.query(StopTime).all()
        assert len(times) == 1
        assert times[0].stop_sequence == 1


# ---------------------------------------------------------------------------
# _parse_calendar
# ---------------------------------------------------------------------------

class TestParseCalendar:
    def test_basic_insert(self, db):
        df = pd.DataFrame([
            {"service_id": "SVC1", "monday": "1", "tuesday": "1", "wednesday": "1",
             "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
             "start_date": "20260101", "end_date": "20261231"},
        ])
        _parse_calendar(df, db)
        db.flush()
        cal = db.query(ServiceCalendar).filter_by(service_id="SVC1").first()
        assert cal is not None
        assert cal.monday is True
        assert cal.saturday is False
        assert cal.start_date == "20260101"

    def test_weekend_service(self, db):
        df = pd.DataFrame([
            {"service_id": "WKND", "monday": "0", "tuesday": "0", "wednesday": "0",
             "thursday": "0", "friday": "0", "saturday": "1", "sunday": "1",
             "start_date": "20260601", "end_date": "20260831"},
        ])
        _parse_calendar(df, db)
        db.flush()
        cal = db.query(ServiceCalendar).filter_by(service_id="WKND").first()
        assert cal.saturday is True
        assert cal.sunday is True
        assert cal.monday is False

    def test_clears_existing_calendar(self, db):
        db.add(ServiceCalendar(service_id="OLD", monday=True, tuesday=True, wednesday=True,
                               thursday=True, friday=True, saturday=False, sunday=False,
                               start_date="20250101", end_date="20251231"))
        db.flush()
        df = pd.DataFrame([
            {"service_id": "NEW", "monday": "1", "tuesday": "0", "wednesday": "0",
             "thursday": "0", "friday": "0", "saturday": "0", "sunday": "0",
             "start_date": "20260101", "end_date": "20261231"},
        ])
        _parse_calendar(df, db)
        db.flush()
        svc_ids = [c.service_id for c in db.query(ServiceCalendar).all()]
        assert "OLD" not in svc_ids
        assert "NEW" in svc_ids


# ---------------------------------------------------------------------------
# _parse_calendar_dates
# ---------------------------------------------------------------------------

class TestParseCalendarDates:
    def test_exception_type_1_stored(self, db):
        df = pd.DataFrame([
            {"service_id": "SVC1", "date": "20260101", "exception_type": "1"},
        ])
        _parse_calendar_dates(df, db)
        db.flush()
        exc = db.query(ServiceCalendarDate).filter_by(service_id="SVC1").first()
        assert exc is not None
        assert exc.exception_type == 1

    def test_exception_type_2_stored(self, db):
        # exception_type=2 means service REMOVED — stored in DB, filtered at query time
        df = pd.DataFrame([
            {"service_id": "SVC1", "date": "20260101", "exception_type": "2"},
        ])
        _parse_calendar_dates(df, db)
        db.flush()
        exc = db.query(ServiceCalendarDate).filter_by(service_id="SVC1").first()
        assert exc is not None
        assert exc.exception_type == 2

    def test_both_exception_types_stored(self, db):
        df = pd.DataFrame([
            {"service_id": "SVC1", "date": "20260101", "exception_type": "1"},
            {"service_id": "SVC2", "date": "20260101", "exception_type": "2"},
        ])
        _parse_calendar_dates(df, db)
        db.flush()
        assert db.query(ServiceCalendarDate).count() == 2

    def test_clears_existing_calendar_dates(self, db):
        db.add(ServiceCalendarDate(service_id="OLD", date="20250101", exception_type=2))
        db.flush()
        df = pd.DataFrame([
            {"service_id": "NEW", "date": "20260101", "exception_type": "1"},
        ])
        _parse_calendar_dates(df, db)
        db.flush()
        svc_ids = [e.service_id for e in db.query(ServiceCalendarDate).all()]
        assert "OLD" not in svc_ids
        assert "NEW" in svc_ids


# ---------------------------------------------------------------------------
# parse_and_store — integration via in-memory zip
# ---------------------------------------------------------------------------

def _make_gtfs_zip(include_calendar=True, include_calendar_dates=True) -> bytes:
    """Build a minimal in-memory GTFS zip for integration tests."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "S1,Stop One,43.0,-79.0\n"
            "S2,Stop Two,43.1,-79.1\n"
        )
        zf.writestr("routes.txt",
            "route_id,route_short_name,route_long_name,route_type\n"
            "R1,1,Route One,3\n"
        )
        zf.writestr("trips.txt",
            "trip_id,route_id,service_id,trip_headsign,direction_id,shape_id\n"
            "T1,R1,SVC1,Guelph,0,\n"
            "T_orphan,MISSING_ROUTE,SVC1,,0,\n"
        )
        zf.writestr("stop_times.txt",
            "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n"
            "T1,S1,08:00:00,08:00:00,1\n"
            "T1,S2,08:30:00,08:30:00,2\n"
            "T1,S_GHOST,09:00:00,09:00:00,3\n"  # orphaned stop — should be skipped
        )
        if include_calendar:
            zf.writestr("calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                "start_date,end_date\n"
                "SVC1,1,1,1,1,1,0,0,20260101,20261231\n"
            )
        if include_calendar_dates:
            zf.writestr("calendar_dates.txt",
                "service_id,date,exception_type\n"
                "SVC1,20260704,2\n"  # service removed (holiday)
                "SVC1,20260101,1\n"  # service added
            )
    return buf.getvalue()


class TestParseAndStore:
    def test_full_zip_parses_correctly(self, db):
        parse_and_store(_make_gtfs_zip(), db)
        assert db.query(Stop).count() == 2
        assert db.query(Route).count() == 1
        assert db.query(Trip).count() == 1          # T_orphan filtered out
        assert db.query(StopTime).count() == 2      # S_GHOST filtered out
        assert db.query(ServiceCalendar).count() == 1
        assert db.query(ServiceCalendarDate).count() == 2

    def test_orphaned_trip_excluded(self, db):
        parse_and_store(_make_gtfs_zip(), db)
        trip_ids = [t.trip_id for t in db.query(Trip).all()]
        assert "T_orphan" not in trip_ids
        assert "T1" in trip_ids

    def test_orphaned_stop_time_excluded(self, db):
        parse_and_store(_make_gtfs_zip(), db)
        stop_ids = [st.stop_id for st in db.query(StopTime).all()]
        assert "S_GHOST" not in stop_ids

    def test_missing_calendar_txt_is_optional(self, db):
        parse_and_store(_make_gtfs_zip(include_calendar=False), db)
        assert db.query(ServiceCalendar).count() == 0

    def test_missing_calendar_dates_txt_is_optional(self, db):
        parse_and_store(_make_gtfs_zip(include_calendar_dates=False), db)
        assert db.query(ServiceCalendarDate).count() == 0

    def test_exception_type_2_stored_for_query_time_filtering(self, db):
        parse_and_store(_make_gtfs_zip(), db)
        removed = db.query(ServiceCalendarDate).filter_by(exception_type=2).first()
        assert removed is not None
        assert removed.date == "20260704"


# ---------------------------------------------------------------------------
# download_gtfs_zip
# ---------------------------------------------------------------------------

class TestDownloadGtfsZip:
    @pytest.mark.anyio
    async def test_missing_url_raises_value_error(self):
        from ingestion.gtfs_static import download_gtfs_zip
        with pytest.raises(ValueError, match="GTFS_STATIC_URL"):
            await download_gtfs_zip(url="")

    @pytest.mark.anyio
    async def test_http_error_propagates(self):
        import httpx
        from ingestion.gtfs_static import download_gtfs_zip

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )

        with patch("ingestion.gtfs_static.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(httpx.HTTPStatusError):
                await download_gtfs_zip(url="https://example.com/gtfs.zip")

    @pytest.mark.anyio
    async def test_successful_download_returns_bytes(self, tmp_path):
        import ingestion.gtfs_static as static_mod
        from ingestion.gtfs_static import download_gtfs_zip

        fake_zip = b"PK fake zip content"
        mock_resp = MagicMock()
        mock_resp.content = fake_zip
        mock_resp.raise_for_status = MagicMock()

        # Redirect GTFS_ZIP_PATH to tmp_path so we don't write to the real data dir
        with (
            patch.object(static_mod, "GTFS_ZIP_PATH", tmp_path / "gtfs.zip"),
            patch("ingestion.gtfs_static.httpx.AsyncClient") as mock_cls,
        ):
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await download_gtfs_zip(url="https://example.com/gtfs.zip")

        assert result == fake_zip
