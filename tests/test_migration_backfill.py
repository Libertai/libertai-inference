"""Migration A (uuid identity add + backfill) data-integrity test.

Runs the REAL alembic migrations on a throwaway database: build the pre-migration
(address-keyed) schema, seed rows, then apply Migration A and assert that every
user got a UUID, a wallet_connection was created with the right chain, and the
child rows were re-pointed to user_id without losing data.
"""

import os

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url

PRE_MIGRATION_REV = "2bccd793c8f4"  # head before Migration A (drop agents/subscriptions)
MIGRATION_A_REV = "ce9e82ee761a"

BASE_ADDR = "0xAbC0000000000000000000000000000000000001"
SOL_ADDR = "SoLanaTestAddr1111111111111111111111111111"


def _scratch_url() -> str:
    base = make_url(os.environ["DATABASE_URL"])
    return base.set(database=f"{base.database}_mig").render_as_string(hide_password=False)


def _admin_conninfo(url) -> str:
    return (
        f"host={url.host} port={url.port or 5432} "
        f"user={url.username} password={url.password} dbname=postgres"
    )


def _libpq_conninfo(url) -> str:
    return (
        f"host={url.host} port={url.port or 5432} "
        f"user={url.username} password={url.password} dbname={url.database}"
    )


@pytest.fixture
def scratch_db():
    """Create a clean scratch database, yield its URL, drop it afterwards."""
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


def _alembic_upgrade(rev: str) -> None:
    # env.py reads DATABASE_URL from the environment (set by the scratch_db fixture).
    command.upgrade(Config("alembic.ini"), rev)


def test_migration_a_backfills_identity(scratch_db):
    # 1. Build the pre-migration (address-keyed) schema.
    _alembic_upgrade(PRE_MIGRATION_REV)

    # 2. Seed address-keyed rows: two users (base + solana), each with a credit txn + api key.
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        for addr in (BASE_ADDR, SOL_ADDR):
            conn.execute("INSERT INTO users (address, created_at) VALUES (%s, now())", (addr,))
            conn.execute(
                "INSERT INTO credit_transactions "
                "(id, address, amount, amount_left, provider, created_at, is_active, status) "
                "VALUES (gen_random_uuid(), %s, 10, 10, 'thirdweb', now(), true, 'completed')",
                (addr,),
            )
            conn.execute(
                "INSERT INTO api_keys (id, key, name, user_address, created_at, is_active, type) "
                "VALUES (gen_random_uuid(), %s, %s, %s, now(), true, 'api')",
                (f"key-{addr}", f"name-{addr}", addr),
            )

    # 3. Apply Migration A.
    _alembic_upgrade(MIGRATION_A_REV)

    # 4. Assert the backfill.
    with psycopg.connect(_libpq_conninfo(scratch_db)) as conn:
        # every user has a UUID id
        assert conn.execute("SELECT count(*) FROM users WHERE id IS NULL").fetchone()[0] == 0

        # one wallet_connection per address, with the chain inferred from the prefix
        chain_base = conn.execute(
            "SELECT chain FROM wallet_connections WHERE address = %s", (BASE_ADDR,)
        ).fetchone()[0]
        chain_sol = conn.execute(
            "SELECT chain FROM wallet_connections WHERE address = %s", (SOL_ADDR,)
        ).fetchone()[0]
        assert chain_base == "base"
        assert chain_sol == "solana"
        assert conn.execute("SELECT count(*) FROM wallet_connections WHERE is_primary").fetchone()[0] == 2

        # child rows re-pointed to the matching user_id (no orphans)
        assert conn.execute("SELECT count(*) FROM credit_transactions WHERE user_id IS NULL").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM api_keys WHERE user_id IS NULL").fetchone()[0] == 0
        mismatched = conn.execute(
            "SELECT count(*) FROM credit_transactions c JOIN users u ON c.address = u.address "
            "WHERE c.user_id <> u.id"
        ).fetchone()[0]
        assert mismatched == 0
