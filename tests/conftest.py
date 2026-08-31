"""Pytest harness for libertai-inference.

The app builds its async engine + ``AsyncSessionLocal`` at import time from
``config.DATABASE_URL`` (see ``src/models/base.py``), and routes use
``async with AsyncSessionLocal()`` directly (no ``get_db`` dependency to
override). So we must point ``DATABASE_URL`` at a **separate test database**
*before* importing anything from ``src``.

The test DB reuses the local dev Postgres (started via ``scripts/dev.sh``):
same host/credentials as ``DATABASE_URL`` but with the database name suffixed
``_test`` (override with ``TEST_DATABASE_URL``). It is created if missing and
its schema is built from the SQLAlchemy models each session.
"""

import os
import time

import psycopg
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Naive TIMESTAMP columns hold UTC, and dedup/expiry compare a DB ``current_timestamp``
# against a Python ``datetime.now()`` — the two clocks must agree. Deployments run UTC;
# a developer machine on any other offset shifts every Python-side "now" and breaks those
# comparisons across a boundary (a month-start dedup window resends, a fresh window reads
# as expired). Set before src, so nothing caches the old zone.
os.environ["TZ"] = "UTC"
time.tzset()

# --- Resolve the test database URL and force it into the env BEFORE importing src ---
load_dotenv()  # does not override already-set env vars


def _resolve_test_db_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return os.path.expandvars(explicit)
    base = os.environ.get("DATABASE_URL")
    if not base:
        raise RuntimeError("DATABASE_URL not set. Start the dev DB (scripts/dev.sh) or set TEST_DATABASE_URL.")
    url = make_url(os.path.expandvars(base))
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


TEST_DATABASE_URL = _resolve_test_db_url()
# Make the app bind its engine/session to the test DB when src is imported below.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def _ensure_test_database_exists() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = url.set(drivername="postgresql")  # plain libpq for the sync psycopg connect
    conninfo = (
        f"host={admin.host} port={admin.port or 5432} user={admin.username} password={admin.password} dbname=postgres"
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{url.database}"')


_ensure_test_database_exists()

# --- Import models AFTER the env is set so metadata is complete and bound to the test DB ---
# Import every model module so Base.metadata knows all tables (mirrors alembic/env.py).
import src.models.anon_chat_usage
import src.models.api_key
import src.models.auth_code
import src.models.blocked_email_domain
import src.models.chat_request
import src.models.credit_transaction
import src.models.entitlement_window
import src.models.inference_call
import src.models.liberclaw_credit_grant
import src.models.liberclaw_user
import src.models.lifecycle_email_send
import src.models.magic_link
import src.models.oauth_connection
import src.models.plan_subscription
import src.models.plan_subscription_event
import src.models.session
import src.models.user
import src.models.wallet_connection  # noqa: F401
from src.models.base import Base

_engine = create_async_engine(make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg"))


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema():
    """Build a fresh schema for the test session, drop it afterwards."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await _engine.dispose()


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    """An AsyncSession wrapped in a transaction that is rolled back after each test.

    ``join_transaction_mode="create_savepoint"`` makes the session's own commit()/rollback()
    ride a SAVEPOINT on top of ``trans`` instead of ending it — needed for code under test that
    manages its own commit boundaries (e.g. a per-user commit/rollback loop); without it, a
    mid-test ``rollback()`` tears down ``trans`` itself and later queries fail with
    "transaction already deassociated from connection".
    """
    async with _engine.connect() as conn:
        trans = await conn.begin()
        session_maker = async_sessionmaker(
            bind=conn, expire_on_commit=False, class_=AsyncSession, join_transaction_mode="create_savepoint"
        )
        async with session_maker() as session:
            yield session
        await trans.rollback()


@pytest_asyncio.fixture
async def async_client():
    """HTTP client bound to the FastAPI app (which uses the test DB via AsyncSessionLocal).

    Imported lazily so app import is not required for pure-service tests. Route tests
    using this fixture should clean up rows they create (no per-test rollback here).
    """
    from httpx import ASGITransport, AsyncClient

    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
