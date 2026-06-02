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

import psycopg
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# --- Resolve the test database URL and force it into the env BEFORE importing src ---
load_dotenv()  # does not override already-set env vars


def _resolve_test_db_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return os.path.expandvars(explicit)
    base = os.environ.get("DATABASE_URL")
    if not base:
        raise RuntimeError(
            "DATABASE_URL not set. Start the dev DB (scripts/dev.sh) or set TEST_DATABASE_URL."
        )
    url = make_url(os.path.expandvars(base))
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


TEST_DATABASE_URL = _resolve_test_db_url()
# Make the app bind its engine/session to the test DB when src is imported below.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def _ensure_test_database_exists() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = url.set(drivername="postgresql")  # plain libpq for the sync psycopg connect
    conninfo = (
        f"host={admin.host} port={admin.port or 5432} "
        f"user={admin.username} password={admin.password} dbname=postgres"
    )
    with psycopg.connect(conninfo, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{url.database}"')


_ensure_test_database_exists()

# --- Import models AFTER the env is set so metadata is complete and bound to the test DB ---
from src.models.base import Base  # noqa: E402

# Import every model module so Base.metadata knows all tables (mirrors alembic/env.py).
import src.models.api_key  # noqa: E402,F401
import src.models.chat_request  # noqa: E402,F401
import src.models.credit_transaction  # noqa: E402,F401
import src.models.inference_call  # noqa: E402,F401
import src.models.liberclaw_user  # noqa: E402,F401
import src.models.user  # noqa: E402,F401

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
    """An AsyncSession wrapped in a transaction that is rolled back after each test."""
    async with _engine.connect() as conn:
        trans = await conn.begin()
        session_maker = async_sessionmaker(bind=conn, expire_on_commit=False, class_=AsyncSession)
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
