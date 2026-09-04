"""Migration script tests: LC side is a plain scratch Postgres database with Core tables
shaped like liberclaw's ``database/models.py`` (subscriptions/subscription_events/users) —
the inference side is the shared test database (see tests/conftest.py), which the script
reads from ``config.DATABASE_URL`` exactly like the real app.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, make_url, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from scripts import migrate_liberclaw_subscriptions as migrate_mod
from scripts.migrate_liberclaw_subscriptions import main, parse_args, run
from src.config import config
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.services.payments.manager import PaymentManager
from src.subscription_tiers import PRODUCT_LIBERCLAW
from tests.test_payment_manager import FakeProvider
from tests.test_payment_manager_liberclaw import _lc_user

PS = PlanSubscription.__table__
PSE = PlanSubscriptionEvent.__table__
LU = LiberclawUser.__table__


def _lc_scratch_url() -> str:
    base = make_url(config.DATABASE_URL)
    return base.set(database=f"{base.database}_lcmig", drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )


def _admin_conninfo(url) -> str:
    return f"host={url.host} port={url.port or 5432} user={url.username} password={url.password} dbname=postgres"


@dataclass
class LC:
    engine: sa.Engine
    subs: sa.Table
    events: sa.Table
    users: sa.Table

    @property
    def url(self) -> str:
        # str(URL) redacts the password as "***" — must render it explicitly for a real DSN.
        return self.engine.url.render_as_string(hide_password=False)

    def insert_user(self, *, user_id=None, email=..., tier="free") -> uuid.UUID:
        user_id = user_id or uuid.uuid4()
        # A unique default per call — liberclaw_users.user_id+user_type is unique, and LC's own
        # users table would never have two rows sharing an email in practice.
        if email is ...:
            email = f"user-{user_id}@example.com"
        with self.engine.begin() as conn:
            conn.execute(self.users.insert().values(id=user_id, email=email, tier=tier))
        return user_id

    def insert_sub(self, *, user_id, id=None, **overrides) -> uuid.UUID:
        sub_id = id or uuid.uuid4()
        now = datetime.now(timezone.utc)
        values = {
            "id": sub_id,
            "user_id": user_id,
            "tier": "starter",
            "status": "active",
            "provider": "revolut",
            "provider_subscription_id": None,
            "provider_customer_id": None,
            "currency": None,
            "current_period_start": None,
            "current_period_end": None,
            "cancel_at_period_end": False,
            "pending_tier": None,
            "is_trial": False,
            "trial_granted_by": None,
            "created_at": now,
            "updated_at": now,
        }
        values.update(overrides)
        with self.engine.begin() as conn:
            conn.execute(self.subs.insert().values(**values))
        return sub_id

    def update_sub(self, sub_id, **overrides) -> None:
        with self.engine.begin() as conn:
            conn.execute(self.subs.update().where(self.subs.c.id == sub_id).values(**overrides))

    def insert_event(self, sub_id, *, id=None, event_type="activated", provider_event_id=None, metadata=None):
        event_id = id or uuid.uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                self.events.insert().values(
                    id=event_id,
                    subscription_id=sub_id,
                    event_type=event_type,
                    provider_event_id=provider_event_id,
                    metadata=metadata,
                    created_at=datetime.now(timezone.utc),
                )
            )
        return event_id


@pytest.fixture(scope="module")
def _lc_scratch_engine():
    url = make_url(_lc_scratch_url())
    admin = url.set(drivername="postgresql")
    with psycopg.connect(_admin_conninfo(admin), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{url.database}"')
    engine = create_engine(url)
    metadata = sa.MetaData()
    users = sa.Table(
        "users",
        metadata,
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String, nullable=True),
        sa.Column("tier", sa.String, nullable=False, server_default="free"),
    )
    subs = sa.Table(
        "subscriptions",
        metadata,
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("tier", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("provider", sa.String, nullable=False),
        sa.Column("provider_subscription_id", sa.String, nullable=True),
        sa.Column("provider_customer_id", sa.String, nullable=True),
        sa.Column("currency", sa.String, nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("pending_tier", sa.String, nullable=True),
        sa.Column("is_trial", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("trial_granted_by", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    events = sa.Table(
        "subscription_events",
        metadata,
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("subscription_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("provider_event_id", sa.String, nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    yield engine, subs, events, users
    engine.dispose()
    with psycopg.connect(_admin_conninfo(admin), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')


@pytest.fixture
def lc(_lc_scratch_engine) -> LC:
    engine, subs, events, users = _lc_scratch_engine
    with engine.begin() as conn:
        conn.execute(sa.text("TRUNCATE subscription_events, subscriptions, users CASCADE"))
    return LC(engine=engine, subs=subs, events=events, users=users)


@pytest.fixture
def inf_engine():
    engine = create_engine(make_url(config.DATABASE_URL).set(drivername="postgresql+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_inference_liberclaw_rows(inf_engine):
    # Real commits (not the ORM-session savepoint other tests roll back), so this suite must
    # clean up its own product='liberclaw' + liberclaw_users rows before AND after every test.
    def _clean():
        with inf_engine.begin() as conn:
            conn.execute(sa.delete(PS).where(PS.c.product == "liberclaw"))
            conn.execute(sa.delete(LU))

    _clean()
    yield
    _clean()


class FakeRevolutProvider:
    def __init__(self, state: str):
        self.state = state
        self.calls: list[str] = []

    async def get_subscription(self, provider_subscription_id: str):
        self.calls.append(provider_subscription_id)
        return SimpleNamespace(state=self.state)


class RaisingRevolutProvider:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls: list[str] = []

    async def get_subscription(self, provider_subscription_id: str):
        self.calls.append(provider_subscription_id)
        raise self.exc


# ---- argument validation ----


def test_skip_revolut_check_requires_dry_run():
    with pytest.raises(SystemExit):
        parse_args(["--lc-dsn=postgresql://x/y", "--skip-revolut-check"])


def test_delta_requires_watermark():
    with pytest.raises(SystemExit):
        parse_args(["--lc-dsn=postgresql://x/y", "--delta"])


def test_skip_revolut_check_with_dry_run_is_valid():
    args = parse_args(["--lc-dsn=postgresql://x/y", "--dry-run", "--skip-revolut-check"])
    assert args.dry_run and args.skip_revolut_check


# ---- rule 1: verbatim copy ----


async def test_first_copy_preserves_verbatim_timestamps_bypassing_onupdate(lc, inf_engine):
    created = datetime(2023, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    updated = datetime(2023, 3, 5, 12, 30, 45, tzinfo=timezone.utc)
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(user_id=user_id, created_at=created, updated_at=updated)

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))
    assert report["inserted"] == 1

    with inf_engine.begin() as conn:
        row = conn.execute(sa.select(PS).where(PS.c.id == sub_id)).one()
    # A verbatim Core insert reads back the exact old LC value — an ORM insert/update path
    # with `onupdate` would instead stamp wall-clock "now", which this old backdated value is not.
    assert row.created_at == created.replace(tzinfo=None)
    assert row.updated_at == updated.replace(tzinfo=None)
    assert row.liberclaw_account_id == user_id
    assert row.product == "liberclaw"
    assert row.user_id is None


async def test_null_currency_defaults_to_eur(lc, inf_engine):
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(user_id=user_id, currency=None)

    await run(parse_args([f"--lc-dsn={lc.url}"]))

    with inf_engine.begin() as conn:
        currency = conn.execute(sa.select(PS.c.currency).where(PS.c.id == sub_id)).scalar_one()
    assert currency == "EUR"


# ---- rule 2: delta ----


async def test_delta_update_guarded_by_updated_at(lc, inf_engine):
    old_updated = datetime(2024, 1, 1, tzinfo=timezone.utc)
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(user_id=user_id, status="active", updated_at=old_updated, created_at=old_updated)
    await run(parse_args([f"--lc-dsn={lc.url}"]))

    # LC moves on...
    new_updated = datetime(2024, 6, 1, tzinfo=timezone.utc)
    lc.update_sub(sub_id, status="overdue", updated_at=new_updated)

    # ...but the inference row was independently bumped past LC's new updated_at.
    with inf_engine.begin() as conn:
        conn.execute(
            sa.update(PS).where(PS.c.id == sub_id).values(status="cancelled", updated_at=datetime(2024, 7, 1))
        )

    # A watermark after LC's own change means "both modified since watermark" never applies —
    # this isolates the plain per-statement guard from the diff-classification branch.
    far_future_watermark = datetime(2030, 1, 1).isoformat()
    report = await run(parse_args([f"--lc-dsn={lc.url}", "--delta", f"--watermark={far_future_watermark}"]))

    assert report["updated"] == 0
    assert report["diff"] == []
    with inf_engine.begin() as conn:
        status = conn.execute(sa.select(PS.c.status).where(PS.c.id == sub_id)).scalar_one()
    assert status == "cancelled"  # untouched — LC's "overdue" never landed


async def test_delta_both_sides_modified_since_watermark_reported_untouched(lc, inf_engine):
    old_updated = datetime(2024, 1, 1, tzinfo=timezone.utc)
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(user_id=user_id, status="active", updated_at=old_updated, created_at=old_updated)
    first_report = await run(parse_args([f"--lc-dsn={lc.url}"]))
    watermark = first_report["watermark"]

    new_updated = datetime(2024, 6, 1, tzinfo=timezone.utc)
    lc.update_sub(sub_id, status="overdue", updated_at=new_updated)
    with inf_engine.begin() as conn:
        conn.execute(
            sa.update(PS).where(PS.c.id == sub_id).values(status="cancelled", updated_at=datetime(2024, 5, 1))
        )

    report = await run(parse_args([f"--lc-dsn={lc.url}", "--delta", f"--watermark={watermark}"]))

    assert report["updated"] == 0
    assert report["diff"] == [str(sub_id)]
    with inf_engine.begin() as conn:
        status = conn.execute(sa.select(PS.c.status).where(PS.c.id == sub_id)).scalar_one()
    assert status == "cancelled"  # touch nothing


async def test_delta_inserts_missing_and_updates_newer(lc, inf_engine):
    old_updated = datetime(2024, 1, 1, tzinfo=timezone.utc)
    user_a = lc.insert_user()
    sub_a = lc.insert_sub(user_id=user_a, status="active", updated_at=old_updated, created_at=old_updated)
    first_report = await run(parse_args([f"--lc-dsn={lc.url}"]))
    watermark = first_report["watermark"]

    new_updated = datetime(2024, 6, 1, tzinfo=timezone.utc)
    lc.update_sub(sub_a, status="overdue", updated_at=new_updated)

    user_b = lc.insert_user()
    sub_b = lc.insert_sub(user_id=user_b, status="active")

    report = await run(parse_args([f"--lc-dsn={lc.url}", "--delta", f"--watermark={watermark}"]))
    assert report["inserted"] == 1
    assert report["updated"] == 1
    assert report["diff"] == []

    with inf_engine.begin() as conn:
        status_a = conn.execute(sa.select(PS.c.status).where(PS.c.id == sub_a)).scalar_one()
        status_b = conn.execute(sa.select(PS.c.status).where(PS.c.id == sub_b)).scalar_one()
    assert status_a == "overdue"
    assert status_b == "active"


async def test_delta_does_not_report_blocked_update_as_translated(lc, inf_engine):
    """Minor 2: wind_down_translated must reflect only what THIS run's copy actually wrote —
    a translation candidate whose UPDATE was blocked by the delta guard (inference
    independently newer) must not be reported as translated."""
    old_updated = datetime(2024, 1, 1, tzinfo=timezone.utc)
    user_id = lc.insert_user(tier="pro")
    sub_id = lc.insert_sub(user_id=user_id, status="active", updated_at=old_updated, created_at=old_updated)
    await run(parse_args([f"--lc-dsn={lc.url}"]))

    period_end = datetime.now(timezone.utc) + timedelta(days=10)
    new_updated = datetime(2024, 6, 1, tzinfo=timezone.utc)
    lc.update_sub(
        sub_id,
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=period_end,
        updated_at=new_updated,
    )
    with inf_engine.begin() as conn:
        conn.execute(sa.update(PS).where(PS.c.id == sub_id).values(updated_at=datetime(2024, 7, 1)))

    far_future_watermark = datetime(2030, 1, 1).isoformat()
    report = await run(parse_args([f"--lc-dsn={lc.url}", "--delta", f"--watermark={far_future_watermark}"]))

    assert report["updated"] == 0
    assert report["wind_down_translated"] == []


# ---- rule 3: events ----


async def test_event_on_conflict_do_nothing_no_crash_no_dup(lc, inf_engine):
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(user_id=user_id)
    lc.insert_event(sub_id, event_type="activated", provider_event_id="ORDER_COMPLETED:xyz")

    r1 = await run(parse_args([f"--lc-dsn={lc.url}"]))
    assert r1["inserted_events"] == 1
    r2 = await run(parse_args([f"--lc-dsn={lc.url}"]))
    assert r2["inserted_events"] == 0

    with inf_engine.begin() as conn:
        count = conn.execute(
            sa.select(sa.func.count()).select_from(PSE).where(PSE.c.provider_event_id == "ORDER_COMPLETED:xyz")
        ).scalar_one()
    assert count == 1


# ---- rule 4: revolut check ----


async def test_swallowed_cancel_reported_and_not_marked_cancelled(lc, inf_engine, monkeypatch, capsys):
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_sub_1",
        cancel_at_period_end=True,
    )
    fake = FakeRevolutProvider(state="active")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["revolut"]["swallowed"] == [str(sub_id)]
    assert report["revolut"]["confirmed_cancelled"] == 0
    assert fake.calls == ["rev_sub_1"]
    with inf_engine.begin() as conn:
        provider_cancelled = conn.execute(sa.select(PS.c.provider_cancelled).where(PS.c.id == sub_id)).scalar_one()
    assert provider_cancelled is False
    out = capsys.readouterr().out
    assert "WARNING" in out and str(sub_id) in out


async def test_confirmed_cancel_marks_provider_cancelled_true(lc, inf_engine, monkeypatch):
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="cancelled",
        provider="revolut",
        provider_subscription_id="rev_sub_2",
        cancel_at_period_end=True,
    )
    fake = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["revolut"]["confirmed_cancelled"] == 1
    with inf_engine.begin() as conn:
        provider_cancelled = conn.execute(sa.select(PS.c.provider_cancelled).where(PS.c.id == sub_id)).scalar_one()
    assert provider_cancelled is True


async def test_revolut_check_preserves_verbatim_updated_at(lc, inf_engine, monkeypatch):
    """C1 regression: the Core UPDATE that writes provider_cancelled must also carry the exact
    LC updated_at already written by the copy — omitting it would let the column's `onupdate`
    stamp wall-clock now(), the exact onupdate trap rule 1 exists to prevent, and would then
    land the row on every future delta's manual-diff list.
    """
    old_updated = datetime(2023, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_sub_verbatim",
        cancel_at_period_end=True,
        created_at=old_updated,
        updated_at=old_updated,
    )
    fake = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    await run(parse_args([f"--lc-dsn={lc.url}"]))

    with inf_engine.begin() as conn:
        row = conn.execute(sa.select(PS.c.provider_cancelled, PS.c.updated_at).where(PS.c.id == sub_id)).one()
    assert row.provider_cancelled is True
    assert row.updated_at == old_updated.replace(tzinfo=None)


async def test_revolut_check_request_failure_is_unknown_not_aborted(lc, inf_engine, monkeypatch, capsys):
    """I2: an HTTPError/timeout from the Revolut GET must never abort the run — the row is
    reported as unknown and left exactly as the copy step wrote it."""
    old_updated = datetime(2023, 7, 1, tzinfo=timezone.utc)
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_sub_flaky",
        cancel_at_period_end=True,
        created_at=old_updated,
        updated_at=old_updated,
    )
    fake = RaisingRevolutProvider(httpx.ConnectTimeout("boom"))
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["revolut"]["unknown"] == [str(sub_id)]
    assert report["revolut"]["confirmed_cancelled"] == 0
    assert report["revolut"]["swallowed"] == []
    with inf_engine.begin() as conn:
        row = conn.execute(sa.select(PS.c.provider_cancelled, PS.c.updated_at).where(PS.c.id == sub_id)).one()
    assert row.provider_cancelled is False
    assert row.updated_at == old_updated.replace(tzinfo=None)
    assert "WARNING" in capsys.readouterr().out


def _copy_lc_into_inference(lc: LC, inf_engine) -> None:
    """Directly drive first_copy against inf_engine, bypassing the Revolut pass entirely —
    simulates a process that crashed after the copy committed but before the check ran."""
    with lc.engine.connect() as lc_conn:
        lc_subs = lc_conn.execute(sa.select(lc.subs)).all()
        lc_events = lc_conn.execute(sa.select(lc.events)).all()
    _, live_account_ids = migrate_mod._lc_subscribed_and_live_ids(lc_subs)
    now = migrate_mod.now_naive_utc()
    with inf_engine.begin() as inf_conn:
        migrate_mod.first_copy(
            inf_conn, lc_subs, lc_events, migrate_mod._wind_down_ids(lc_subs, now, live_account_ids)
        )


async def test_revolut_pass_is_durable_and_rerunnable_after_a_crash(lc, inf_engine, monkeypatch):
    """N1 regression: gating candidates on this run's own touched_ids means a crash between the
    copy commit and the Revolut pass (or any later plain re-run) finds nothing to check and
    exits green, provider_cancelled never verified. Candidates must be derived from durable
    state (id join on LC's flag + inference's still-False provider_cancelled), not run-local
    memory of what this invocation copied.
    """
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_crash",
        cancel_at_period_end=True,
    )
    _copy_lc_into_inference(lc, inf_engine)  # "crash" here: committed, Revolut pass never ran

    fake = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    # This plain re-run's OWN copy step touches 0 new rows (already exists) — the Revolut pass
    # must still find and check the pending row via durable state.
    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["inserted"] == 0
    assert report["revolut"]["confirmed_cancelled"] == 1
    with inf_engine.begin() as conn:
        provider_cancelled = conn.execute(sa.select(PS.c.provider_cancelled).where(PS.c.id == sub_id)).scalar_one()
    assert provider_cancelled is True


async def test_unknown_revolut_row_is_rechecked_on_next_run(lc, inf_engine, monkeypatch):
    """N1: a row that came back 'unknown' (request failure) must be retried on the next run —
    provider_cancelled stayed False, so it's still a durable candidate."""
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_retry",
        cancel_at_period_end=True,
    )
    failing = RaisingRevolutProvider(httpx.ConnectTimeout("boom"))
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: failing)
    report1 = await run(parse_args([f"--lc-dsn={lc.url}"]))
    assert report1["revolut"]["unknown"] == [str(sub_id)]

    working = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: working)
    report2 = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report2["revolut"]["confirmed_cancelled"] == 1
    with inf_engine.begin() as conn:
        provider_cancelled = conn.execute(sa.select(PS.c.provider_cancelled).where(PS.c.id == sub_id)).scalar_one()
    assert provider_cancelled is True


async def test_revolut_only_mode_checks_without_copying(lc, inf_engine, monkeypatch):
    """N1: --revolut-only runs just the check pass against durable state, valid without --delta."""
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_only",
        cancel_at_period_end=True,
    )
    _copy_lc_into_inference(lc, inf_engine)

    fake = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}", "--revolut-only"]))

    assert report["mode"] == "revolut-only"
    assert report["revolut"]["confirmed_cancelled"] == 1
    assert "bridge" not in report
    with inf_engine.begin() as conn:
        provider_cancelled = conn.execute(sa.select(PS.c.provider_cancelled).where(PS.c.id == sub_id)).scalar_one()
    assert provider_cancelled is True


def test_revolut_only_does_not_require_watermark():
    args = parse_args(["--lc-dsn=postgresql://x/y", "--revolut-only"])
    assert args.revolut_only is True


async def test_revolut_check_lost_race_does_not_stomp_webhook_write(lc, inf_engine, monkeypatch):
    """N3: an optimistic guard on the per-row UPDATE — if a live webhook already wrote
    provider_cancelled (and bumped updated_at) between the GET and our write, the stale GET
    answer must never overwrite it; the row is reported as a lost race instead."""
    old_updated = datetime(2023, 8, 1, tzinfo=timezone.utc)
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_race",
        cancel_at_period_end=True,
        created_at=old_updated,
        updated_at=old_updated,
    )
    _copy_lc_into_inference(lc, inf_engine)

    class WebhookWinsProvider:
        async def get_subscription(self, provider_subscription_id: str):
            # Simulate a webhook racing in and updating the row for real BEFORE our own
            # optimistic UPDATE lands, bumping updated_at away from the value we read.
            with inf_engine.begin() as conn:
                conn.execute(
                    sa.update(PS)
                    .where(PS.c.id == sub_id)
                    .values(provider_cancelled=True, updated_at=datetime(2026, 1, 1))
                )
            return SimpleNamespace(state="active")

    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: WebhookWinsProvider())

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["revolut"]["lost_race"] == [str(sub_id)]
    with inf_engine.begin() as conn:
        row = conn.execute(sa.select(PS.c.provider_cancelled, PS.c.updated_at).where(PS.c.id == sub_id)).one()
    assert row.provider_cancelled is True  # the webhook's write survives untouched
    assert row.updated_at == datetime(2026, 1, 1)


async def test_revolut_check_converges_when_lc_updated_at_has_moved_since_copy(lc, inf_engine, monkeypatch):
    """NEW-1 regression: the optimistic guard must compare against INFERENCE's stored
    updated_at, not LC's current one — otherwise a plain LC-side updated_at bump (unrelated to
    the row's copied fields, with no delta re-sync in between) makes the guard mismatch
    forever, so --revolut-only never converges: every check reports a false lost_race even
    with zero real webhook contention.
    """
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_converge",
        cancel_at_period_end=True,
    )
    _copy_lc_into_inference(lc, inf_engine)  # T0: copy commits

    # T1: LC's own updated_at moves on — inference is NOT re-synced (no delta ran), so its
    # stored updated_at is now stale relative to LC's current value.
    lc.update_sub(sub_id, updated_at=datetime.now(timezone.utc) + timedelta(hours=1))

    fake = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}", "--revolut-only"]))

    assert report["revolut"]["lost_race"] == []
    assert report["revolut"]["confirmed_cancelled"] == 1
    with inf_engine.begin() as conn:
        provider_cancelled = conn.execute(sa.select(PS.c.provider_cancelled).where(PS.c.id == sub_id)).scalar_one()
    assert provider_cancelled is True


async def test_revolut_only_prints_warning_for_unresolved_rows_but_exits_zero(lc, inf_engine, monkeypatch, capsys):
    """Minor 4: --revolut-only never silently exits green when rows are left unresolved
    (unknown/lost_race) — a top-level WARNING always calls it out, even though the exit code
    itself stays 0 (operator-decides posture, matching rule 4 generally)."""
    user_id = lc.insert_user()
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="active",
        provider="revolut",
        provider_subscription_id="rev_unresolved",
        cancel_at_period_end=True,
    )
    _copy_lc_into_inference(lc, inf_engine)
    failing = RaisingRevolutProvider(httpx.ConnectTimeout("boom"))
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: failing)

    exit_code = await main([f"--lc-dsn={lc.url}", "--revolut-only"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert str(sub_id) in out


def test_is_wind_down_raises_on_naive_current_period_end():
    """LC's column is timezone=True — a naive value feeding this control-flow branch is schema
    drift and must fail loudly, not silently mis-decide via astimezone()'s local-time
    assumption. An explicit raise, not an assert: `python -O` strips asserts, and this is a
    money migration."""
    m = {
        "status": "cancelled",
        "cancel_at_period_end": True,
        "current_period_end": datetime(2030, 1, 1),  # naive
        "user_id": uuid.uuid4(),
    }
    with pytest.raises(ValueError):
        migrate_mod._is_wind_down(m, migrate_mod.now_naive_utc(), set())


# ---- rule 5: override synthesis ----


async def test_override_synthesized_for_paid_tier_user_with_no_live_sub(lc, inf_engine):
    user_id = lc.insert_user(email="paid@example.com", tier="pro")

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["overrides_created"] == 1
    with inf_engine.begin() as conn:
        sub = conn.execute(sa.select(PS).where(PS.c.liberclaw_account_id == user_id)).one()
        event = conn.execute(sa.select(PSE).where(PSE.c.subscription_id == sub.id)).one()
        bridge = conn.execute(sa.select(LU).where(LU.c.liberclaw_account_id == user_id)).one()
    assert sub.status == "active"
    assert sub.provider == "manual"
    assert sub.current_period_end is None
    assert sub.is_trial is False
    assert event.event_type == "override_migrated"
    assert event.metadata_json == {"source": "migration", "lc_tier": "pro", "trial_granted_by": None}
    assert bridge.user_id == "paid@example.com"
    assert bridge.user_type == "email"
    assert bridge.tier == "pro"


async def test_no_override_when_free_tier_or_live_sub_exists(lc, inf_engine):
    free_user = lc.insert_user(tier="free")
    live_user = lc.insert_user(tier="pro")
    lc.insert_sub(user_id=live_user, status="active")

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["overrides_created"] == 0
    with inf_engine.begin() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(PS).where(PS.c.provider == "manual")).scalar_one()
    assert count == 0
    _ = free_user


async def test_no_override_for_winding_down_payer_with_cancelled_sub(lc, inf_engine):
    """C2 regression: LC keeps users.tier paid while a cancelled/expired subscription's period
    runs out — a naive "paid tier AND no LIVE row" predicate would synthesize a free-forever
    manual override for this winding-down payer. Holding ANY LC subscription row (any status)
    must exclude them, and the exclusion is reported for the operator.
    """
    user_id = lc.insert_user(tier="pro")
    lc.insert_sub(user_id=user_id, status="cancelled")

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["overrides_created"] == 0
    assert report["wind_down_excluded"] == [str(user_id)]
    with inf_engine.begin() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(PS).where(PS.c.provider == "manual")).scalar_one()
    assert count == 0


async def test_verify_passes_when_eligible_user_has_native_live_inference_row(lc, inf_engine, capsys):
    """N2 regression: a paid-tier LC user with no LC subscription row but an already-live
    NATIVE inference row (e.g. a post-cutover /liberclaw signup, unrelated to this migration)
    is properly billed already — must not count as an override gap in verify."""
    user_id = lc.insert_user(email="native@example.com", tier="pro")
    with inf_engine.begin() as conn:
        conn.execute(
            sa.insert(PS).values(
                id=uuid.uuid4(),
                user_id=None,
                tier="pro",
                status="active",
                provider="revolut",
                provider_subscription_id="native_sub_1",
                provider_customer_id=None,
                currency="EUR",
                current_period_start=None,
                current_period_end=None,
                cancel_at_period_end=False,
                pending_tier=None,
                is_trial=False,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                product="liberclaw",
                liberclaw_account_id=user_id,
                provider_cancelled=False,
            )
        )

    exit_code = await main([f"--lc-dsn={lc.url}"])

    assert exit_code == 0
    assert "FAIL" not in capsys.readouterr().out


# ---- rule 6: bridge completion (C3) ----


async def test_bridge_updates_legacy_null_account_id_row_in_place(lc, inf_engine):
    """C3 regression: a legacy pre-billing bridge row (liberclaw_account_id NULL, a documented
    production reality) must be UPDATEd in place on an email match — any API key already FK'd
    to its id then inherits the bridge. Resolving only by account_id would either crash on the
    (user_id, user_type) unique constraint or, worse, insert a second keyless row that no API
    key ever points to (stale-tier entitlement stuck on the legacy row).
    """
    user_id = lc.insert_user(email="legacy@example.com", tier="pro")
    legacy_id = uuid.uuid4()
    with inf_engine.begin() as conn:
        conn.execute(
            sa.insert(LU).values(
                id=legacy_id,
                user_id="legacy@example.com",
                user_type="email",
                tier="free",
                liberclaw_account_id=None,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
    lc.insert_sub(user_id=user_id, status="active")

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["bridge"]["updated"] == 1
    assert report["bridge"]["created"] == 0
    with inf_engine.begin() as conn:
        rows = conn.execute(sa.select(LU).where(LU.c.user_id == "legacy@example.com")).all()
    assert len(rows) == 1  # updated in place, never duplicated
    assert rows[0].id == legacy_id
    assert rows[0].liberclaw_account_id == user_id
    assert rows[0].tier == "pro"


async def test_bridge_conflict_never_reassigns_a_bound_row(lc, inf_engine, capsys):
    """N5 regression: an email that resolves to a row ALREADY bound to a DIFFERENT account id
    is a genuine identity conflict — never silently reassigned (which would steal the bridge
    from the account it actually belongs to). Reported instead, no write.
    """
    user_id = lc.insert_user(email="shared@example.com", tier="pro")
    other_account_id = uuid.uuid4()
    bound_row_id = uuid.uuid4()
    with inf_engine.begin() as conn:
        conn.execute(
            sa.insert(LU).values(
                id=bound_row_id,
                user_id="shared@example.com",
                user_type="email",
                tier="starter",
                liberclaw_account_id=other_account_id,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
    lc.insert_sub(user_id=user_id, status="active")

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["bridge"]["created"] == 0
    assert report["bridge"]["updated"] == 0
    assert len(report["bridge"]["conflicts"]) == 1
    with inf_engine.begin() as conn:
        rows = conn.execute(sa.select(LU).where(LU.c.user_id == "shared@example.com")).all()
    assert len(rows) == 1
    assert rows[0].id == bound_row_id
    assert rows[0].liberclaw_account_id == other_account_id  # untouched
    assert rows[0].tier == "starter"  # untouched
    assert "WARNING" in capsys.readouterr().out


# ---- rule 7: verify ----


async def test_verify_failure_causes_nonzero_exit(lc, capsys):
    user_id = lc.insert_user(email=None, tier="free")
    lc.insert_sub(user_id=user_id, status="active")

    exit_code = await main([f"--lc-dsn={lc.url}"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out


async def test_verify_passes_for_clean_migration(lc, capsys):
    user_id = lc.insert_user(email="ok@example.com", tier="free")
    lc.insert_sub(user_id=user_id, status="active")

    exit_code = await main([f"--lc-dsn={lc.url}"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out


async def test_aborts_before_any_write_on_lc_upgrading_status(lc, inf_engine, capsys):
    """C4 regression: LC's parked status is literally 'upgrading' (not inference's own
    'pending_upgrade'). The check aborts the run rather than reporting a failure after the
    copy, so no such row is ever written."""
    user_id = lc.insert_user(email="ok@example.com", tier="free")
    sub_id = lc.insert_sub(user_id=user_id, status="upgrading")

    with pytest.raises(SystemExit) as excinfo:
        await main([f"--lc-dsn={lc.url}"])

    assert str(sub_id) in str(excinfo.value)
    assert "FAIL zero upgrading rows" in capsys.readouterr().out
    with inf_engine.begin() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(PS).where(PS.c.id == sub_id)).scalar_one() == 0


async def test_verify_source_checks_run_under_dry_run(lc, capsys):
    """The source-side checks (no email, zero upgrading, wind-down report) must run even under
    --dry-run, since they need no committed inference state at all."""
    user_id = lc.insert_user(email=None, tier="free")
    lc.insert_sub(user_id=user_id, status="active")

    exit_code = await main([f"--lc-dsn={lc.url}", "--dry-run", "--skip-revolut-check"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FAIL no subscribed LC user lacks email" in out


async def test_verify_does_not_false_fail_on_manual_diff_row(lc):
    """I1 regression: a manual-diff row (both sides modified since the watermark) makes LC's
    and inference's current status genuinely, correctly diverge — the per-status comparison
    must exempt it instead of false-FAILing every delta that protects one."""
    old_updated = datetime(2024, 1, 1, tzinfo=timezone.utc)
    user_id = lc.insert_user(email="ok@example.com", tier="free")
    sub_id = lc.insert_sub(user_id=user_id, status="active", updated_at=old_updated, created_at=old_updated)
    first_report = await run(parse_args([f"--lc-dsn={lc.url}"]))
    watermark = first_report["watermark"]

    new_updated = datetime(2024, 6, 1, tzinfo=timezone.utc)
    lc.update_sub(sub_id, status="overdue", updated_at=new_updated)
    engine = create_engine(make_url(config.DATABASE_URL).set(drivername="postgresql+psycopg"))
    with engine.begin() as conn:
        conn.execute(
            sa.update(PS).where(PS.c.id == sub_id).values(status="cancelled", updated_at=datetime(2024, 5, 1))
        )
    engine.dispose()

    report = await run(parse_args([f"--lc-dsn={lc.url}", "--delta", f"--watermark={watermark}"]))

    assert report["diff"] == [str(sub_id)]
    assert report["verify_ok"] is True


async def test_verify_ignores_manual_row_for_account_unknown_to_lc(lc, inf_engine, capsys):
    """The bridge check's target set must match what bridge_completion can actually act on —
    LC-known accounts only. A manual+active liberclaw row whose account LC's users table has
    never heard of (a post-cutover trial, an admin override) is out of this migration's scope;
    demanding a bridge row for it would fail verify forever with nothing the script can do.
    """
    user_id = lc.insert_user(email="ok@example.com", tier="free")
    lc.insert_sub(user_id=user_id, status="active")
    foreign_account_id = uuid.uuid4()
    with inf_engine.begin() as conn:
        conn.execute(
            sa.insert(PS).values(
                id=uuid.uuid4(),
                user_id=None,
                tier="pro",
                status="active",
                provider="manual",
                provider_subscription_id=None,
                provider_customer_id=None,
                currency="EUR",
                current_period_start=None,
                current_period_end=None,
                cancel_at_period_end=False,
                pending_tier=None,
                is_trial=True,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                product="liberclaw",
                liberclaw_account_id=foreign_account_id,
                provider_cancelled=False,
            )
        )

    exit_code = await main([f"--lc-dsn={lc.url}"])

    assert exit_code == 0
    assert "FAIL" not in capsys.readouterr().out


async def test_verify_target_flags_paid_user_with_no_live_coverage_at_all(inf_engine):
    """Minor 1: the override-coverage check is an existence test (some live row, any
    provider), not a count-equality — a genuinely uncovered paid-tier LC user (no LC
    subscription, no live inference row of any kind) must be flagged directly, rather than a
    fragile scoped-count comparison that can go vacuously trivial."""
    lc_user_row = SimpleNamespace(_mapping={"id": uuid.uuid4(), "tier": "pro", "email": "x@example.com"})
    with inf_engine.begin() as conn:
        ok = migrate_mod.verify_target(conn, [], [lc_user_row], {"pro"}, set(), set())
    assert ok is False


# ---- N4: wind-down translation ----


async def test_wind_down_row_translated_shape_on_copy(lc, inf_engine):
    """N4 controller ruling (NEW-3): LC represents "cancel effective at period end" by flipping
    status to 'cancelled' immediately while cancel_at_period_end stays set and the period runs
    out later — the opposite of inference's own deferred-cancel shape. Translate on copy so
    inference's own expiry pass (not this script) demotes the tier natively at period end.
    provider_cancelled stays False on copy — the translation never pre-declares it; rule 4's
    durable Revolut pass confirms (or swallows) it independently, inspected directly here by
    bypassing that pass entirely (via a low-level first_copy call).
    """
    period_end = datetime.now(timezone.utc) + timedelta(days=10)
    user_id = lc.insert_user(tier="pro")
    sub_id = lc.insert_sub(
        user_id=user_id,
        tier="pro",
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=period_end,
        provider_subscription_id="psub_wind_down_copy",
    )
    _copy_lc_into_inference(lc, inf_engine)

    with inf_engine.begin() as conn:
        row = conn.execute(sa.select(PS).where(PS.c.id == sub_id)).one()
    assert row.status == "active"
    assert row.cancel_at_period_end is True  # verbatim
    assert row.provider_cancelled is False  # never pre-declared
    assert row.tier == "pro"  # verbatim
    assert row.current_period_end == period_end.replace(tzinfo=None)  # verbatim


async def test_translated_row_confirmed_cancelled_by_revolut_pass(lc, inf_engine, monkeypatch):
    """NEW-3: a translated row is a normal durable Revolut candidate (cancel_at_period_end=True,
    provider_cancelled=False) — a confirmed 'cancelled' state marks it True exactly like any
    other row, through the same rule-4 pipeline (not the translation itself)."""
    period_end = datetime.now(timezone.utc) + timedelta(days=10)
    user_id = lc.insert_user(tier="pro")
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=period_end,
        provider_subscription_id="psub_wind_confirm",
    )
    fake = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["wind_down_translated"] == [str(sub_id)]
    assert report["revolut"]["confirmed_cancelled"] == 1
    with inf_engine.begin() as conn:
        row = conn.execute(sa.select(PS.c.status, PS.c.provider_cancelled).where(PS.c.id == sub_id)).one()
    assert row.status == "active"
    assert row.provider_cancelled is True


async def test_translated_row_swallowed_when_revolut_disagrees(lc, inf_engine, monkeypatch, capsys):
    """NEW-3: rule 4's swallowed-cancel detection stays intact for a translated row too — LC
    claiming a wind-down that Revolut doesn't confirm is exactly the swallowed-cancel case."""
    period_end = datetime.now(timezone.utc) + timedelta(days=10)
    user_id = lc.insert_user(tier="pro")
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=period_end,
        provider_subscription_id="psub_wind_swallow",
    )
    fake = FakeRevolutProvider(state="active")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["revolut"]["swallowed"] == [str(sub_id)]
    with inf_engine.begin() as conn:
        row = conn.execute(sa.select(PS.c.status, PS.c.provider_cancelled).where(PS.c.id == sub_id)).one()
    assert row.status == "active"
    assert row.provider_cancelled is False
    assert "WARNING" in capsys.readouterr().out


async def test_wind_down_not_translated_when_account_has_newer_live_row(lc, inf_engine, monkeypatch):
    """NEW-2 regression: LC permits a cancel-then-resubscribe pair for the same account
    ('cancelled' sits outside LC's own live set) — translating the wind-down row too would
    collide two 'active' rows on uq_one_active_plan_subscription_lclw (silently dropping the
    paying row, or FK-aborting its events). Refined ruling: only translate when the account
    holds no OTHER live LC row; otherwise the wind-down row copies verbatim as 'cancelled' —
    the live row governs entitlement.
    """
    user_id = lc.insert_user(tier="pro")
    period_end = datetime.now(timezone.utc) + timedelta(days=10)
    wind_down_id = lc.insert_sub(
        user_id=user_id,
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=period_end,
        provider_subscription_id="psub_old",
    )
    live_id = lc.insert_sub(user_id=user_id, status="active", provider_subscription_id="psub_new")
    fake = FakeRevolutProvider(state="active")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["wind_down_translated"] == []
    with inf_engine.begin() as conn:
        wind_down_status = conn.execute(sa.select(PS.c.status).where(PS.c.id == wind_down_id)).scalar_one()
        live_status = conn.execute(sa.select(PS.c.status).where(PS.c.id == live_id)).scalar_one()
        live_count = conn.execute(
            sa.select(sa.func.count())
            .select_from(PS)
            .where(PS.c.liberclaw_account_id == user_id, PS.c.status.in_(("pending", "active", "overdue")))
        ).scalar_one()
    assert wind_down_status == "cancelled"
    assert live_status == "active"
    assert live_count == 1  # exactly one live row — no partial-unique collision


async def test_only_the_latest_wind_down_row_is_translated(lc, inf_engine, monkeypatch):
    """A cancel -> resubscribe -> cancel-again account holds SEVERAL wind-down rows at once (all
    'cancelled' with a future period end, none live). At most one may be translated — two
    'active' rows for the same account collide on uq_one_active_plan_subscription_lclw, silently
    dropping one row. The longest-running period wins; the rest copy verbatim as 'cancelled'.
    """
    user_id = lc.insert_user(email="ok@example.com", tier="pro")
    now = datetime.now(timezone.utc)
    earlier_id = lc.insert_sub(
        user_id=user_id,
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=now + timedelta(days=5),
        provider_subscription_id="psub_wd_earlier",
    )
    later_id = lc.insert_sub(
        user_id=user_id,
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=now + timedelta(days=40),
        provider_subscription_id="psub_wd_later",
    )
    lc.insert_event(earlier_id, event_type="activated")
    lc.insert_event(later_id, event_type="activated")
    fake = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["inserted"] == 2  # both rows land — nothing silently dropped
    assert report["wind_down_translated"] == [str(later_id)]
    assert report["verify_ok"] is True
    with inf_engine.begin() as conn:
        statuses = dict(conn.execute(sa.select(PS.c.id, PS.c.status).where(PS.c.id.in_([earlier_id, later_id]))).all())
    assert statuses[later_id] == "active"
    assert statuses[earlier_id] == "cancelled"


async def test_delta_re_elects_wind_down_winner_without_collision(lc, inf_engine, monkeypatch):
    """The elected wind-down row can change between runs: a later wind-down row appears and
    takes the account. The previous winner still sits 'active' in inference, so the new
    winner's write collides on uq_one_active_plan_subscription_lclw unless the loser is
    demoted first. The loser's own LC updated_at need not have moved at all, so the delta's
    updated_at guard cannot carry the demotion — it is election-derived state.

    The demotion therefore carries the loser's WHOLE LC state, not just its status: it writes
    the LC updated_at, which then blocks that row's guarded update in the same run.
    """
    old = datetime(2024, 1, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    user_id = lc.insert_user(email="ok@example.com", tier="pro")
    earlier_id = lc.insert_sub(
        user_id=user_id,
        tier="starter",
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=now + timedelta(days=5),
        provider_subscription_id="psub_re_elect_earlier",
        created_at=old,
        updated_at=old,
    )
    lc.insert_event(earlier_id, event_type="activated")
    fake = FakeRevolutProvider(state="cancelled")
    monkeypatch.setattr(migrate_mod.payment_registry, "get", lambda name: fake)

    first_report = await run(parse_args([f"--lc-dsn={lc.url}"]))
    assert first_report["wind_down_translated"] == [str(earlier_id)]

    # A later wind-down row appears, and the loser's own tier moved in LC meanwhile.
    loser_updated = datetime(2024, 6, 1, tzinfo=timezone.utc)
    lc.update_sub(earlier_id, tier="pro", updated_at=loser_updated)
    later_id = lc.insert_sub(
        user_id=user_id,
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=now + timedelta(days=40),
        provider_subscription_id="psub_re_elect_later",
    )
    lc.insert_event(later_id, event_type="activated")

    report = await run(parse_args([f"--lc-dsn={lc.url}", "--delta", f"--watermark={first_report['watermark']}"]))

    assert report["re_elected"] == 1
    assert report["inserted"] == 1
    assert report["wind_down_translated"] == [str(later_id)]
    assert report["verify_ok"] is True
    with inf_engine.begin() as conn:
        rows = conn.execute(
            sa.select(PS.c.id, PS.c.status, PS.c.tier, PS.c.updated_at).where(PS.c.id.in_([earlier_id, later_id]))
        ).all()
    by_id = {row.id: row for row in rows}
    assert by_id[earlier_id].status == "cancelled"
    assert by_id[later_id].status == "active"
    # The demotion carries the whole LC state, so the loser's tier change syncs in this same
    # run — the guarded update it would otherwise need is blocked by the demotion's own write.
    assert by_id[earlier_id].tier == "pro"
    # The demotion must not let the column onupdate stamp wall-clock time: a bumped
    # updated_at defeats the guarded-update comparison and the next delta's watermark math.
    assert by_id[earlier_id].updated_at == loser_updated.astimezone(timezone.utc).replace(tzinfo=None)


async def test_no_translation_once_period_has_already_ended(lc, inf_engine):
    """The translation only applies while the period genuinely hasn't ended yet — once it has,
    LC's raw 'cancelled' status copies verbatim (inference's own expiry pass already owns it
    by then, having presumably run before this migration script ever saw the row)."""
    period_end = datetime.now(timezone.utc) - timedelta(days=1)
    user_id = lc.insert_user(tier="pro")
    sub_id = lc.insert_sub(
        user_id=user_id,
        status="cancelled",
        cancel_at_period_end=True,
        current_period_end=period_end,
    )

    report = await run(parse_args([f"--lc-dsn={lc.url}"]))

    assert report["wind_down_translated"] == []
    with inf_engine.begin() as conn:
        row = conn.execute(sa.select(PS.c.status, PS.c.provider_cancelled).where(PS.c.id == sub_id)).one()
    assert row.status == "cancelled"
    assert row.provider_cancelled is False


async def test_verify_per_status_accounts_for_translation(lc, capsys):
    """N4: per-status verify must compare against each row's TRANSLATED status, not LC's raw
    status — a wind-down row legitimately sits 'active' on the target."""
    period_end = datetime.now(timezone.utc) + timedelta(days=10)
    user_id = lc.insert_user(email="ok@example.com", tier="pro")
    sub_id = lc.insert_sub(
        user_id=user_id, status="cancelled", cancel_at_period_end=True, current_period_end=period_end
    )
    lc.insert_event(sub_id, event_type="activated")

    exit_code = await main([f"--lc-dsn={lc.url}"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out


async def test_translated_wind_down_row_is_demoted_by_expiry_pass(db, monkeypatch):
    """A translated wind-down row AS THE COPY WRITES IT (status='active',
    cancel_at_period_end=True, provider_cancelled=False, all else verbatim) must be
    indistinguishable from any other deferred-cancel row to inference's own expiry pass —
    check_expirations cancels it at the provider and demotes the LC user's tier at period end
    exactly as for a native deferred cancel."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    account_id = uuid.uuid4()
    await _lc_user(db, account_id, tier="pro")
    sub = PlanSubscription(
        user_id=None,
        tier="pro",
        provider="revolut",
        status="active",
        provider_subscription_id="psub_wind_down_expiry",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
        cancel_at_period_end=True,
        provider_cancelled=False,
        current_period_end=datetime.now() - timedelta(days=2),
    )
    db.add(sub)
    await db.flush()

    provider = FakeProvider()
    provider.cycle_end_days = -3  # the provider agrees the cycle is over, authorising the cancel
    mgr = PaymentManager(provider, db)
    await mgr.check_expirations()

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "expired"
    assert provider.cancelled == ["psub_wind_down_expiry"]  # pass 0 owns the provider-side cancel

    lc_user = (
        await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
    ).scalar_one()
    assert lc_user.tier == "free"


# ---- idempotency ----


async def test_idempotent_rerun_inserts_zero_second_time(lc):
    live_user = lc.insert_user(tier="pro")
    lc.insert_sub(user_id=live_user, status="active")
    lc.insert_event(lc.insert_sub(user_id=lc.insert_user(tier="free")), event_type="activated")
    override_user = lc.insert_user(tier="pro")

    args = parse_args([f"--lc-dsn={lc.url}"])
    report1 = await run(args)
    assert report1["inserted"] >= 1
    assert report1["overrides_created"] == 1

    report2 = await run(args)
    assert report2["inserted"] == 0
    assert report2["inserted_events"] == 0
    assert report2["overrides_created"] == 0
    assert report2["bridge"]["created"] == 0
    _ = override_user


# ---- dry-run ----


async def test_dry_run_touches_nothing(lc, inf_engine):
    user_id = lc.insert_user(tier="pro")
    lc.insert_sub(user_id=user_id, status="active", cancel_at_period_end=True, provider_subscription_id="rev_x")

    report = await run(parse_args([f"--lc-dsn={lc.url}", "--dry-run", "--skip-revolut-check"]))

    assert report["inserted"] == 1  # reported...
    with inf_engine.begin() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(PS)).scalar_one()
    assert count == 0  # ...but never committed
