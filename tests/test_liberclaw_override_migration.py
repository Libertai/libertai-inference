"""Migration test for the liberclaw grandfathered-cap backfill (runs real alembic)."""

import importlib.util
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, make_url

from alembic import command

REVISION = "5e21a0dfd95e"
PREV = "c4d8e1f2a3b7"
ROOT = Path(__file__).parent.parent
MIGRATION = ROOT / "alembic" / "versions" / f"{REVISION}_liberclaw_credits_limit_override.py"
PRECHECK = ROOT / "scripts" / "liberclaw_repricing_precheck.sql"


def _scratch_url() -> str:
    base = make_url(os.environ["DATABASE_URL"])
    return base.set(database=f"{base.database}_lcoverride").render_as_string(hide_password=False)


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


def _lc_user(conn, tier: str, *, account_id: uuid.UUID | None = None) -> uuid.UUID:
    lc_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO liberclaw_users (id, user_id, user_type, tier, liberclaw_account_id, created_at) "
        "VALUES (%s, %s, 'email', %s, %s, now())",
        (lc_id, f"{lc_id.hex}@example.com", tier, account_id),
    )
    return lc_id


def _sub(conn, account_id, tier: str, status: str, *, product="liberclaw", provider="revolut", trial=False) -> None:
    conn.execute(
        "INSERT INTO plan_subscriptions (id, user_id, tier, status, provider, product, liberclaw_account_id, "
        "is_trial, cancel_at_period_end, provider_cancelled) VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, false, false)",
        (uuid.uuid4(), tier, status, provider, product, account_id, trial),
    )


def _subscriber(conn, tier: str, status: str, *, sub_tier=None, **sub_kwargs) -> uuid.UUID:
    """A liberclaw_users row bridged to one plan_subscriptions row for the same account."""
    account_id = uuid.uuid4()
    lc_id = _lc_user(conn, tier, account_id=account_id)
    _sub(conn, account_id, sub_tier or tier, status, **sub_kwargs)
    return lc_id


def _overrides(conn) -> dict[uuid.UUID, float | None]:
    return dict(conn.execute("SELECT id, credits_limit_override FROM liberclaw_users").fetchall())


def _tiers(conn) -> dict[uuid.UUID, str]:
    return dict(conn.execute("SELECT id, tier FROM liberclaw_users").fetchall())


def _run_precheck(conn) -> list[list[tuple]]:
    """Each report's rows, from the operator script run statement by statement (psql
    meta-commands and the transaction wrapper stripped)."""
    sql = "\n".join(line for line in PRECHECK.read_text().splitlines() if not line.startswith("\\"))
    statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]
    reports = []
    for statement in statements:
        body = "\n".join(line for line in statement.splitlines() if not line.startswith("--")).strip()
        if body.upper().startswith(("BEGIN", "ROLLBACK")):
            continue
        reports.append(conn.execute(body).fetchall())
    return reports


def _load_migration():
    spec = importlib.util.spec_from_file_location("lc_override_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_grandfathers_live_paid_subscribers_only(scratch_db):
    cfg = Config("alembic.ini")
    command.upgrade(cfg, PREV)

    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        starter = _subscriber(conn, "starter", "active")
        pro_overdue = _subscriber(conn, "pro", "overdue")  # dunning keeps the entitlement
        team_manual = _subscriber(conn, "team", "active", provider="manual")
        # Parked by start_checkout/override_tier while a new checkout is open: still live.
        pro_upgrading = _subscriber(conn, "pro", "upgrading", provider="manual")
        # The live row's tier wins, and lc_users.tier is realigned to it.
        tier_mismatch = _subscriber(conn, "pro", "active", sub_tier="starter")
        stored_free = _subscriber(conn, "free", "active", sub_tier="team")
        # Two live rows (upgrading sits outside the one-live-row index): the highest tier.
        two_live = _subscriber(conn, "starter", "active")
        two_live_account = conn.execute(
            "SELECT liberclaw_account_id FROM liberclaw_users WHERE id = %s", (two_live,)
        ).fetchone()[0]
        _sub(conn, two_live_account, "pro", "upgrading", provider="manual")

        unpaid_checkout = _subscriber(conn, "starter", "pending")
        trial = _subscriber(conn, "pro", "active", provider="manual", trial=True)
        cancelled = _subscriber(conn, "starter", "cancelled")
        expired = _subscriber(conn, "team", "expired")
        free_with_live_row = _subscriber(conn, "free", "active")
        libertai_row = _subscriber(conn, "pro", "active", product="libertai")
        no_bridge = _lc_user(conn, "pro")
        orphan_account = uuid.uuid4()
        _sub(conn, orphan_account, "starter", "active")

        # The operator precheck runs before the column exists, and previews exactly this backfill.
        paid_uncovered, orphans, upgrading, mismatches, planned = _run_precheck(conn)
        tiers_before = _tiers(conn)

    assert {r[0] for r in paid_uncovered} == {unpaid_checkout, trial, cancelled, expired, libertai_row, no_bridge}
    assert [r[1] for r in orphans] == [orphan_account]
    assert len(upgrading) == 2
    assert {r[0]: (r[2], r[3]) for r in mismatches} == {
        tier_mismatch: ("pro", "starter"),
        stored_free: ("free", "team"),
        two_live: ("starter", "pro"),
    }
    planned_by_id = {r[0]: (r[3], float(r[4])) for r in planned}

    command.upgrade(cfg, REVISION)
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        overrides = _overrides(conn)
        tiers = _tiers(conn)
    expected = {
        starter: ("starter", 100.0),
        pro_overdue: ("pro", 500.0),
        team_manual: ("team", 2000.0),
        pro_upgrading: ("pro", 500.0),
        tier_mismatch: ("starter", 100.0),
        stored_free: ("team", 2000.0),
        two_live: ("pro", 500.0),
    }
    assert {lc_id: (tiers[lc_id], overrides[lc_id]) for lc_id in expected} == expected
    assert planned_by_id == expected
    for untouched in (unpaid_checkout, trial, cancelled, expired, free_with_live_row, libertai_row, no_bridge):
        assert overrides[untouched] is None
        assert tiers[untouched] == tiers_before[untouched]

    # Idempotent: a re-run writes nothing, and never overwrites an override already set.
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        conn.execute("UPDATE liberclaw_users SET credits_limit_override = 123 WHERE id = %s", (starter,))
    engine = create_engine(scratch_db.set(drivername="postgresql+psycopg"))
    try:
        with engine.begin() as bind:
            assert _load_migration().backfill(bind) == (0, 0)
    finally:
        engine.dispose()
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        assert _overrides(conn) == {**overrides, starter: 123.0}
        assert _tiers(conn) == tiers

    command.downgrade(cfg, PREV)
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        columns = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'liberclaw_users'"
            ).fetchall()
        }
    assert "credits_limit_override" not in columns
