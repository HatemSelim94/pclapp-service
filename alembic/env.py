"""Alembic environment.

The database URL comes from the app settings (``PCLAPP_DATABASE_URL``), so there is exactly
one place that knows where the database is. Tests can also hand in an open connection via
``config.attributes["connection"]`` to run migrations inside their own transaction.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or str(get_settings().database_url)


def run_migrations_offline() -> None:
    """Emit SQL instead of executing it (``alembic upgrade head --sql``)."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection: Connection | None = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
