"""Test fixtures. Everything runs against a real PostgreSQL; there is no SQLite stand-in.

Two tiers of database fixture:

* ``db`` — the default. Opens one connection, begins a transaction, hands the app a session
  bound to that connection (SQLAlchemy's "join an external transaction" pattern, savepoints
  for the app's own commits) and rolls everything back at the end. Fast, and tests cannot
  see each other's rows.
* ``committed_db`` — for tests that need rows visible from *other* connections: the worker's
  ``SKIP LOCKED`` claim from two threads, or the migration round-trip. These commit for real
  and TRUNCATE every table afterwards.

Settings come from the environment, set below before ``app`` is imported so the cached
``get_settings()`` sees them. Override the database with ``PCLAPP_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

TEST_DATABASE_URL = os.environ.get(
    "PCLAPP_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/pclapp_test",
)
ADMIN_KEY = "test-admin-key"
_DATA_DIR = Path(tempfile.mkdtemp(prefix="pclapp-test-"))

os.environ["PCLAPP_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["PCLAPP_ADMIN_KEY"] = ADMIN_KEY
os.environ["PCLAPP_DATA_DIR"] = str(_DATA_DIR)
os.environ["PCLAPP_WORKER_POLL_SECONDS"] = "0.05"

from app import auth  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base, User  # noqa: E402
from app.storage import get_storage  # noqa: E402
from tests.fake_processor import FakeProcessor  # noqa: E402
from worker.main import Worker  # noqa: E402
from worker.processor import Processor  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str = TEST_DATABASE_URL) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL)
    # Fresh schema for the run, built by the real migrations.
    command.downgrade(alembic_config(), "base")
    command.upgrade(alembic_config(), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def data_dir() -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR


# --- tier 1: rolled back ------------------------------------------------------------------


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.fixture
def client(db: Session, data_dir: Path) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db
        db.commit()  # commits the savepoint only; the outer transaction is rolled back

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


# --- tier 2: committed, truncated afterwards -------------------------------------------------


@pytest.fixture
def committed_db(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def committed_client(committed_db: Session, data_dir: Path) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield committed_db
        committed_db.commit()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


# --- helpers --------------------------------------------------------------------------------


class Actor:
    """A user together with a working API key, ready to make requests."""

    def __init__(self, user: User, key: str) -> None:
        self.user = user
        self.key = key
        self.headers = {"Authorization": f"Bearer {key}"}

    @property
    def id(self) -> str:
        return str(self.user.id)


MakeActor = Callable[[str], Actor]


def _actor_factory(session: Session) -> MakeActor:
    def make(name: str) -> Actor:
        user = User(name=name)
        session.add(user)
        session.flush()
        _, key = auth.issue_key(session, user, label="test")
        session.commit()
        return Actor(user, key)

    return make


@pytest.fixture
def make_actor(db: Session) -> MakeActor:
    return _actor_factory(db)


@pytest.fixture
def make_committed_actor(committed_db: Session) -> MakeActor:
    return _actor_factory(committed_db)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


# --- worker ---------------------------------------------------------------------------------


@pytest.fixture
def make_worker(engine: Engine, data_dir: Path) -> Callable[..., Worker]:
    """A Worker with its own sessions on ``engine`` (so it sees only committed rows)."""

    def make(processor: Processor | None = None, worker_id: str = "test-worker") -> Worker:
        return Worker(
            sessionmaker(bind=engine, expire_on_commit=False),
            processor or FakeProcessor(),
            get_storage(),
            get_settings(),
            worker_id=worker_id,
        )

    return make
