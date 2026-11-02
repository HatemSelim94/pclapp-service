"""Migrations must round-trip on an empty database, and the models must match them."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from app.models import Base
from tests.conftest import alembic_config

EXPECTED_TABLES = {"users", "api_keys", "clouds", "jobs", "clusters", "shares"}
EXPECTED_ENUMS = {"job_type", "job_status", "share_role"}


def _enum_names(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))
        return {row[0] for row in rows}


def test_upgrade_downgrade_upgrade(engine: Engine) -> None:
    cfg = alembic_config()

    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    assert _enum_names(engine) == set(), "downgrade must drop the enum types too"

    command.upgrade(cfg, "head")
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    assert _enum_names(engine) >= EXPECTED_ENUMS

    # a second round proves downgrade left nothing behind that blocks upgrade
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def test_models_match_migrations(engine: Engine) -> None:
    """``alembic check`` as a test: the hand-written migration and the models agree."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], diff
