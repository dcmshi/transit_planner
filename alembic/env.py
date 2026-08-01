"""Alembic environment.

The database URL comes from config.DATABASE_URL (i.e. the same .env the app
reads) rather than alembic.ini, so migrations can never be pointed at a
different database than the application by accident.

GeoAlchemy2's alembic helpers are installed because Stop.geog is a Geography
column on PostgreSQL: without them autogenerate emits an unusable type and
tries to manage the GIST index GeoAlchemy2 creates for itself.
"""

from logging.config import fileConfig

from geoalchemy2 import alembic_helpers
from sqlalchemy import engine_from_config, pool

from alembic import context
from config import DATABASE_URL
from db.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — `alembic upgrade head --sql`."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=alembic_helpers.include_object,
        render_item=alembic_helpers.render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=alembic_helpers.include_object,
            render_item=alembic_helpers.render_item,
            process_revision_directives=alembic_helpers.writer,
            # SQLite cannot ALTER most things in place; batch mode rewrites
            # the table instead.  Harmless on PostgreSQL.
            render_as_batch=DATABASE_URL.startswith("sqlite"),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
