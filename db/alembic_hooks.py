"""Autogenerate filters for Alembic.

Lives here rather than in alembic/env.py so it can be imported and tested —
env.py runs migration context code at import time.
"""

from typing import Any

from geoalchemy2 import alembic_helpers

from db.models import Base

# Object types whose owning table decides whether autogenerate should care.
_TABLE_SCOPED = frozenset({"table", "index", "unique_constraint", "foreign_key_constraint"})


def _owning_table(obj: Any, name: str | None, type_: str) -> str | None:
    if type_ == "table":
        return name
    table = getattr(obj, "table", None)
    return getattr(table, "name", None)


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Limit autogenerate to the tables db/models.py declares.

    PostGIS extensions put their own tables in `public` — postgis_tiger_geocoder
    alone adds around sixty (faces, county, addrfeat, the *_lookup tables …).
    Alembic reflects those, finds no model for them, and proposes dropping
    every one.  Anything reflected that the models do not declare is therefore
    ignored outright; objects that *are* ours fall through to GeoAlchemy2's
    filter, which knows to leave Geography columns and their GIST indexes
    alone.
    """
    if reflected and type_ in _TABLE_SCOPED:
        if _owning_table(obj, name, type_) not in Base.metadata.tables:
            return False
    result: bool = alembic_helpers.include_object(obj, name, type_, reflected, compare_to)
    return result
