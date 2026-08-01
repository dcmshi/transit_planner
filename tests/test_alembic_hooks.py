"""
Unit tests for the Alembic autogenerate filter.

PostGIS extensions put their own tables in `public` — postgis_tiger_geocoder
alone adds around sixty.  Without a filter, autogenerate reflects them, finds
no model, and proposes dropping every one.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table

from db.alembic_hooks import include_object
from db.models import Base

_OTHER = MetaData()
_EXTENSION_TABLE = Table("faces", _OTHER, Column("gid", Integer, primary_key=True))
_APP_TABLE = Base.metadata.tables["stops"]


class TestIncludeObject:

    def test_reflected_extension_table_is_ignored(self):
        assert include_object(_EXTENSION_TABLE, "faces", "table", True, None) is False

    @pytest.mark.parametrize("name", [
        "stops", "routes", "trips", "stop_times",
        "service_calendar", "service_calendar_dates",
        "observed_trips", "reliability_records",
    ])
    def test_reflected_application_tables_are_kept(self, name):
        assert include_object(Base.metadata.tables[name], name, "table", True, None) is True

    def test_index_on_an_extension_table_is_ignored(self):
        index = MagicMock()
        index.table = _EXTENSION_TABLE
        assert include_object(index, "county_lookup_name_idx", "index", True, None) is False

    def test_index_on_an_application_table_is_kept(self):
        index = MagicMock()
        index.table = _APP_TABLE
        assert include_object(index, "ix_trips_route_id", "index", True, None) is True

    def test_unreflected_objects_are_never_filtered_by_name(self):
        """Model-side objects come from Base.metadata by definition; filtering
        them on reflection rules would drop legitimate new tables."""
        new_table = Table("brand_new", _OTHER, Column("id", Integer, primary_key=True))
        assert include_object(new_table, "brand_new", "table", False, None) is True

    def test_columns_still_defer_to_geoalchemy2(self):
        """Non-table-scoped types must reach GeoAlchemy2's filter, which is
        what keeps Geography columns handled correctly."""
        column = Column("stop_name", String())
        assert include_object(column, "stop_name", "column", True, _APP_TABLE) is True
