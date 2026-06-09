"""Migration test for plan_subscriptions + revolut provider (runs real alembic)."""

import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url

REVISION = "f1a2b3c4d5e6"
PREV = "491dd7c0450b"


def _scratch_url() -> str:
    base = make_url(os.environ["DATABASE_URL"])
    return base.set(database=f"{base.database}_paymig").render_as_string(hide_password=False)


def _admin_conninfo(url) -> str:
    return f"host={url.host} port={url.port or 5432} user={url.username} password={url.password} dbname=postgres"


def _libpq_conninfo(url) -> str:
    return f"host={url.host} port={url.port or 5432} user={url.username} password={url.password} dbname={url.database}"


@pytest.fixture
def scratch_db():
    url = make_url(_scratch_url())
    with psycopg.connect(_admin_conninfo(url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{url.database}"')
    prev = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        yield url
    finally:
        os.environ["DATABASE_URL"] = prev
        with psycopg.connect(_admin_conninfo(url), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')


def test_migration_creates_tables_and_enforces_one_active_sub(scratch_db):
    cfg = Config("alembic.ini")
    command.upgrade(cfg, REVISION)

    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        # Tables exist.
        for table in ("plan_subscriptions", "plan_subscription_events"):
            assert conn.execute(
                "SELECT to_regclass(%s)", (f"public.{table}",)
            ).fetchone()[0] is not None

        # revolut is a valid credit provider, with no block number required.
        user_id = uuid.uuid4()
        conn.execute("INSERT INTO users (id, email_verified) VALUES (%s, false)", (user_id,))
        conn.execute(
            "INSERT INTO credit_transactions "
            "(id, user_id, amount, amount_left, provider, is_active, status, created_at) "
            "VALUES (%s, %s, 10, 10, 'revolut', true, 'completed', now())",
            (uuid.uuid4(), user_id),
        )

        # One active subscription per user: the second active insert must fail.
        conn.execute(
            "INSERT INTO plan_subscriptions (id, user_id, tier, status, provider) "
            "VALUES (%s, %s, 'plus', 'active', 'revolut')",
            (uuid.uuid4(), user_id),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO plan_subscriptions (id, user_id, tier, status, provider) "
                "VALUES (%s, %s, 'go', 'active', 'revolut')",
                (uuid.uuid4(), user_id),
            )

    # Downgrade cleanly reverses everything.
    command.downgrade(cfg, PREV)
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('public.plan_subscriptions')").fetchone()[0] is None
