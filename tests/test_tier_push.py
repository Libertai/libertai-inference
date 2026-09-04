"""Snapshot push machinery: session-event collection, post-commit fire, and the retry drain.

Tests that only need to observe the collection/marker side of the hooks use the ``db``
fixture (a savepoint the fixture rolls back — never truly committed) and monkeypatch
``tier_push.push_snapshot`` itself, since a savepoint's rows are invisible to any other
connection (including a fresh ``AsyncSessionLocal()`` push_snapshot would open). Tests that
exercise the real push (its own re-read, ordering, the drain) use ``AsyncSessionLocal``
directly with manual cleanup, mirroring ``tests/test_liberclaw_bridge.py``.
"""

import uuid
from datetime import datetime

import httpx
from sqlalchemy import delete, func, select

from src.config import config
from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.services.payments import tier_push
from src.subscription_tiers import PRODUCT_LIBERCLAW

# asyncio_mode = "auto" (pyproject.toml) picks up async defs without a marker; no pytestmark
# here since this file also has a plain sync test (test_build_snapshot_shape).


def _enable(monkeypatch):
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    monkeypatch.setattr(config, "LIBERCLAW_API_URL", "https://lclw.test")
    monkeypatch.setattr(config, "LIBERCLAW_PUSH_SECRET", "s3cret")


def _lclw_sub(**overrides) -> PlanSubscription:
    kwargs = {
        "user_id": None,
        "tier": "starter",
        "provider": "manual",
        "status": "active",
        "product": PRODUCT_LIBERCLAW,
        "liberclaw_account_id": uuid.uuid4(),
    }
    kwargs.update(overrides)
    return PlanSubscription(**kwargs)


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


async def _await_scheduled():
    """Drain whatever _schedule_push queued during the last commit()."""
    tasks = list(tier_push._pending_tasks)
    for t in tasks:
        await t


async def _pending_marker_in(db, sub_id):
    """Query an already-open session (the ``db`` fixture, or one under an ``async with``).
    ``scalar_one_or_none`` is safe here only where a test knows at most one marker exists —
    markers are no longer deduped at insert time, so a row touched twice in flight can have
    more than one (see ``_pending_marker_ids``)."""
    return (
        await db.execute(
            select(PlanSubscriptionEvent).where(
                PlanSubscriptionEvent.subscription_id == sub_id,
                PlanSubscriptionEvent.event_type == tier_push.PENDING_EVENT_TYPE,
            )
        )
    ).scalar_one_or_none()


async def _pending_marker(sub_id):
    """Query via a fresh, real ``AsyncSessionLocal`` — for tests using real commits."""
    async with AsyncSessionLocal() as db:
        return await _pending_marker_in(db, sub_id)


async def _pending_marker_ids(sub_id) -> set:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(PlanSubscriptionEvent.id).where(
                PlanSubscriptionEvent.subscription_id == sub_id,
                PlanSubscriptionEvent.event_type == tier_push.PENDING_EVENT_TYPE,
            )
        )
        return set(rows.scalars().all())


async def _cleanup(sub_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(PlanSubscriptionEvent).where(PlanSubscriptionEvent.subscription_id == sub_id))
        await db.execute(delete(PlanSubscription).where(PlanSubscription.id == sub_id))
        await db.commit()


# --------------------------------------------------------------------- build_snapshot


def test_build_snapshot_shape():
    sub = _lclw_sub()
    sub.id = uuid.uuid4()
    sub.created_at = datetime(2026, 1, 1)
    sub.updated_at = datetime(2026, 1, 2)
    snapshot = tier_push.build_snapshot(sub)
    assert set(snapshot) == {
        "account_id",
        "subscription_id",
        "tier",
        "status",
        "is_trial",
        "currency",
        "current_period_start",
        "current_period_end",
        "cancel_at_period_end",
        "pending_tier",
        "created_at",
        "snapshot_at",
    }
    assert snapshot["subscription_id"] == str(sub.id)
    assert snapshot["account_id"] == str(sub.liberclaw_account_id)
    # Offset-aware: a naive-UTC value serialized without one skews a client's Date parse
    # by its own local offset (see src/interfaces/common.py's UtcDatetime).
    assert snapshot["snapshot_at"] == "2026-01-02T00:00:00+00:00"
    assert snapshot["created_at"] == "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------- collection (session events)


async def test_insert_triggers_collection(db, monkeypatch):
    """A brand-new liberclaw row (session.new) qualifies unconditionally."""
    _enable(monkeypatch)
    calls = []

    async def fake_push(sub_id):
        calls.append(sub_id)
        return True

    monkeypatch.setattr(tier_push, "push_snapshot", fake_push)

    sub = _lclw_sub()
    db.add(sub)
    await db.commit()
    await _await_scheduled()

    assert calls == [sub.id]
    marker = await _pending_marker_in(db, sub.id)
    assert marker is not None


async def test_dirty_non_snapshot_field_does_not_push(db, monkeypatch):
    """Mutating provider_customer_id (not a snapshot field) must not schedule a push
    or write a new pending marker."""
    _enable(monkeypatch)
    calls = []

    async def fake_push(sub_id):
        calls.append(sub_id)
        return True

    monkeypatch.setattr(tier_push, "push_snapshot", fake_push)

    sub = _lclw_sub()
    db.add(sub)
    await db.commit()
    await _await_scheduled()
    calls.clear()

    marker_count = (
        await db.execute(
            select(func.count())
            .select_from(PlanSubscriptionEvent)
            .where(PlanSubscriptionEvent.subscription_id == sub.id)
        )
    ).scalar_one()

    sub.provider_customer_id = "cus_123"
    await db.commit()
    await _await_scheduled()

    assert calls == []
    marker_count_after = (
        await db.execute(
            select(func.count())
            .select_from(PlanSubscriptionEvent)
            .where(PlanSubscriptionEvent.subscription_id == sub.id)
        )
    ).scalar_one()
    assert marker_count_after == marker_count


async def test_flag_off_noop(db, monkeypatch):
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    calls = []

    async def fake_push(sub_id):
        calls.append(sub_id)
        return True

    monkeypatch.setattr(tier_push, "push_snapshot", fake_push)

    sub = _lclw_sub()
    db.add(sub)
    await db.commit()
    await _await_scheduled()

    assert calls == []
    marker = await _pending_marker_in(db, sub.id)
    assert marker is None


# --------------------------------------------------------------------- real push (fresh session)


async def test_http_failure_leaves_marker(monkeypatch):
    _enable(monkeypatch)

    async def failing_put(snapshot):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(tier_push, "_put_snapshot", failing_put)

    sub = _lclw_sub()
    async with AsyncSessionLocal() as db:
        db.add(sub)
        await db.commit()
        sub_id = sub.id
    try:
        await _await_scheduled()
        marker = await _pending_marker(sub_id)
        assert marker is not None
    finally:
        await _cleanup(sub_id)


async def test_post_commit_ordering(monkeypatch):
    """The push must only ever see the row via a separate connection AFTER the writing
    transaction has actually committed — proven by a second connection being able to see
    the just-written status (would raise NoResultFound if the commit hadn't landed yet,
    under READ COMMITTED)."""
    _enable(monkeypatch)
    seen = {}

    async def fake_put(snapshot):
        async with AsyncSessionLocal() as verify_db:
            status = (
                await verify_db.execute(
                    select(PlanSubscription.status).where(
                        PlanSubscription.id == uuid.UUID(snapshot["subscription_id"])
                    )
                )
            ).scalar_one()
        seen["status"] = status
        return _FakeResponse(200)

    monkeypatch.setattr(tier_push, "_put_snapshot", fake_put)

    sub = _lclw_sub(status="active")
    async with AsyncSessionLocal() as db:
        db.add(sub)
        await db.commit()
        sub_id = sub.id
    try:
        await _await_scheduled()
        assert seen.get("status") == "active"
    finally:
        await _cleanup(sub_id)


async def test_drain_rereads_current_state(monkeypatch):
    """A tier changed between the row being parked and the drain running must push the
    NEW tier — drain never uses a stored payload, it always re-reads live state."""
    _enable(monkeypatch)
    outage = {"on": True}
    captured = []

    async def flaky_put(snapshot):
        if outage["on"]:
            raise RuntimeError("simulated outage")
        captured.append(snapshot)
        return _FakeResponse(200)

    monkeypatch.setattr(tier_push, "_put_snapshot", flaky_put)

    sub = _lclw_sub(tier="starter")
    async with AsyncSessionLocal() as db:
        db.add(sub)
        await db.commit()
        sub_id = sub.id
    try:
        await _await_scheduled()  # parked: the outage left the marker behind

        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(PlanSubscription).where(PlanSubscription.id == sub_id))).scalar_one()
            row.tier = "pro"
            await db.commit()
        await _await_scheduled()  # this commit's own post-commit push also fails; a second marker joins the first

        outage["on"] = False
        async with AsyncSessionLocal() as db:
            drained = await tier_push.drain_pending_tier_pushes(db)

        assert drained == 1
        assert captured
        assert captured[-1]["tier"] == "pro"
        marker = await _pending_marker(sub_id)
        assert marker is None
    finally:
        await _cleanup(sub_id)


async def test_drain_tracks_attempt_count_on_failure(monkeypatch):
    """A drain attempt that fails bumps ``metadata_json['attempts']`` on the marker(s) it
    tried, so a poison marker (one that never succeeds) is visible instead of retried
    silently forever."""
    _enable(monkeypatch)

    async def failing_put(snapshot):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(tier_push, "_put_snapshot", failing_put)

    sub = _lclw_sub()
    async with AsyncSessionLocal() as db:
        db.add(sub)
        await db.commit()
        sub_id = sub.id
    try:
        await _await_scheduled()  # parked with one marker, attempts unset

        async with AsyncSessionLocal() as db:
            drained = await tier_push.drain_pending_tier_pushes(db)
        assert drained == 0

        async with AsyncSessionLocal() as db:
            marker = await _pending_marker_in(db, sub_id)
        assert marker is not None
        assert marker.metadata_json == {"attempts": 1}
    finally:
        await _cleanup(sub_id)


# --------------------------------------------------------------------- IMPORTANT 1: lost-push race


async def test_interleaved_commits_do_not_lose_a_push(monkeypatch):
    """Reproduces the race: push A (for change 1) captures its marker set and starts its
    HTTP call; change 2 commits (parking its own marker) WHILE that call is in flight; push
    A succeeds and must delete only the marker it captured at start, never change 2's —
    otherwise the record that a push is still owed for change 2 is lost until some
    unrelated future change happens to touch the row again.
    """
    _enable(monkeypatch)
    # Commits still park markers, but nothing auto-fires in the background — driving both
    # "pushes" by hand makes the interleaving deterministic instead of a timing gamble.
    monkeypatch.setattr(tier_push, "_schedule_push", lambda sub_id: None)

    sub = _lclw_sub(tier="starter")
    async with AsyncSessionLocal() as db:
        db.add(sub)
        await db.commit()
        sub_id = sub.id
    try:
        marker_ids_after_change1 = await _pending_marker_ids(sub_id)
        assert len(marker_ids_after_change1) == 1

        sent_tiers = []

        async def fake_put_that_races_change2(snapshot):
            # change 2 commits (and parks its own marker) WHILE push A's "HTTP call" for
            # change 1 is in flight — the exact interleaving under review.
            async with AsyncSessionLocal() as db2:
                row = (await db2.execute(select(PlanSubscription).where(PlanSubscription.id == sub_id))).scalar_one()
                row.tier = "pro"
                await db2.commit()
            sent_tiers.append(snapshot["tier"])
            return _FakeResponse(200)

        monkeypatch.setattr(tier_push, "_put_snapshot", fake_put_that_races_change2)

        assert await tier_push.push_snapshot(sub_id) is True

        # Push A read+sent change 1's state (before change 2 committed), then deleted only
        # the marker it had captured at its own start.
        assert sent_tiers == ["starter"]
        remaining = await _pending_marker_ids(sub_id)
        assert len(remaining) == 1
        assert remaining != marker_ids_after_change1  # change 2's marker, not change 1's

        # The retry drain (standing in for "some later push") still finds and clears it,
        # carrying change 2's up-to-date tier — nothing was permanently lost.
        final_tiers = []

        async def final_put(snapshot):
            final_tiers.append(snapshot["tier"])
            return _FakeResponse(200)

        monkeypatch.setattr(tier_push, "_put_snapshot", final_put)
        async with AsyncSessionLocal() as db:
            drained = await tier_push.drain_pending_tier_pushes(db)

        assert drained == 1
        assert final_tiers == ["pro"]
        assert await _pending_marker_ids(sub_id) == set()
    finally:
        await _cleanup(sub_id)


# --------------------------------------------------------------------- IMPORTANT 2: no idle-in-transaction


async def test_no_open_transaction_during_http_call(monkeypatch):
    """The session that read the row for the payload must be committed (and its
    transaction ended) BEFORE the HTTP call — never held open across it, which would sit
    idle-in-transaction on a pooled connection for the length of the call."""
    from src.models import base as base_module

    _enable(monkeypatch)
    seen_sessions = []
    real_sessionmaker = base_module.AsyncSessionLocal

    def spying_sessionmaker():
        session = real_sessionmaker()
        seen_sessions.append(session)
        return session

    monkeypatch.setattr(base_module, "AsyncSessionLocal", spying_sessionmaker)

    result = {}

    async def fake_put(snapshot):
        read_session = seen_sessions[0]
        result["in_transaction"] = read_session.in_transaction()
        return _FakeResponse(200)

    monkeypatch.setattr(tier_push, "_put_snapshot", fake_put)

    sub = _lclw_sub()
    async with AsyncSessionLocal() as db:
        db.add(sub)
        await db.commit()
        sub_id = sub.id
    try:
        await _await_scheduled()
        assert seen_sessions, "push_snapshot never opened a session via the spied factory"
        assert result.get("in_transaction") is False
    finally:
        await _cleanup(sub_id)
