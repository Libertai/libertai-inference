"""Snapshot push: LCLW subscription state pushed to LiberClaw's backend on every change.

Mechanism (session events registered in src/models/base.py):
1. ``after_flush`` collects ids of ``product='liberclaw'`` rows whose snapshot fields just
   changed, and write-ahead-parks a ``tier_push_pending`` marker for each (same flush). No
   dedup against an existing marker: two changes in flight for the same row each get their
   own marker row, so an earlier push's cleanup can never delete a later change's marker
   before it has been sent (see ``push_snapshot``). The queue stays bounded by deletes, not
   by insert-time dedup.
2. ``after_commit`` fires the HTTP push per collected id as a background task.
3. On a 2xx, exactly the markers ``push_snapshot`` observed at its own start are deleted; on
   any failure they are left for ``drain_pending_tier_pushes``.

The push always re-reads the row from a *fresh* session — never the caller's (closed by the
time a background task runs, and possibly stale by the time the retry drain runs) — and never
holds that session open across the HTTP call itself (a held session across a slow PUT is an
idle-in-transaction backend on a pooled connection).
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import delete, func, insert, inspect, select
from sqlalchemy.orm import Session as SyncSession

from src.config import config
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.subscription_tiers import PRODUCT_LIBERCLAW
from src.utils.logger import setup_logger
from src.utils.pg_locks import LCLW_TIER_PUSH_DRAIN_LOCK_ID, single_runner

logger = setup_logger(__name__)

PENDING_EVENT_TYPE = "tier_push_pending"
# Depth of this query (COUNT of distinct pending sub ids) is the drain's observability metric.
DRAIN_BATCH_LIMIT = 50
# A marker still unpushed after this many drain attempts is logged as likely poison — no
# drop logic: the periodic pull-reconcile (out of scope here) is the eventual net.
DRAIN_ATTEMPT_WARN_THRESHOLD = 5
_SESSION_INFO_KEY = "lclw_push"

# The definition of "snapshot changed", for both the push hook and the payload shape.
# created_at/updated_at are excluded: the former never changes, the latter is emitted
# as snapshot_at unconditionally rather than diffed.
SNAPSHOT_FIELDS = (
    "tier",
    "status",
    "is_trial",
    "currency",
    "current_period_start",
    "current_period_end",
    "cancel_at_period_end",
    "pending_tier",
)

# Fire-and-forget tasks (asyncio.create_task) are only weakly held by the event loop;
# without a strong reference here one can be garbage-collected mid-flight.
_pending_tasks: set[asyncio.Task] = set()

_warned_unconfigured = False


def push_ready() -> bool:
    """False (no-op everywhere) unless the flag is on AND the receiver is configured.
    An unconfigured URL/secret with the flag on warns once rather than per-flush."""
    global _warned_unconfigured
    if not config.LIBERCLAW_BILLING_ENABLED:
        return False
    if not config.LIBERCLAW_API_URL or not config.LIBERCLAW_PUSH_SECRET:
        if not _warned_unconfigured:
            logger.warning("LIBERCLAW_API_URL/LIBERCLAW_PUSH_SECRET unset; snapshot push disabled")
            _warned_unconfigured = True
        return False
    return True


def utc_iso(dt: datetime) -> str:
    """Naive columns hold UTC; emit an offset so a client's ``Date`` parse can't skew by its
    own local zone (mirrors ``src.interfaces.common.UtcDatetime``'s convention)."""
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat()


def build_snapshot(sub: PlanSubscription) -> dict[str, Any]:
    """Canonical LCLW subscription-state payload. Cross-repo contract: PR-C's receiver
    builds its Pydantic model from this exact key set — never rename or drop a key here."""
    return {
        "account_id": str(sub.liberclaw_account_id),
        "subscription_id": str(sub.id),
        "tier": sub.tier,
        "status": sub.status,
        "is_trial": sub.is_trial,
        "currency": sub.currency,
        "current_period_start": utc_iso(sub.current_period_start) if sub.current_period_start else None,
        "current_period_end": utc_iso(sub.current_period_end) if sub.current_period_end else None,
        "cancel_at_period_end": sub.cancel_at_period_end,
        "pending_tier": sub.pending_tier,
        "created_at": utc_iso(sub.created_at),
        "snapshot_at": utc_iso(sub.updated_at),
    }


def _snapshot_field_changed(obj: PlanSubscription) -> bool:
    state = inspect(obj)
    if state is None:
        # Not expected for a mapped, persistent PlanSubscription — skip rather than push a
        # snapshot for an object we can't confirm actually changed.
        logger.error("tier_push: inspect() returned no state for a PlanSubscription; skipping")
        return False
    return any(state.attrs[field].history.added for field in SNAPSHOT_FIELDS)


def collect_snapshot_pushes(session: SyncSession) -> None:
    """``after_flush`` hook: mark every ``product='liberclaw'`` row whose snapshot fields
    just changed for a post-commit push (``session.info[_SESSION_INFO_KEY]``), and
    write-ahead a ``tier_push_pending`` marker for each in the same flush — a crash before
    the HTTP push still leaves the marker for the retry drain to find.

    Uses Core ``session.execute()`` (not ``session.add()`` + a nested ``flush()``, which
    would re-enter this same handler): the flush already in progress is still open for more
    SQL, just not for another unit-of-work pass.
    """
    if not push_ready():
        return
    changed_ids: set[uuid.UUID] = set()
    for obj in session.new:
        if isinstance(obj, PlanSubscription) and obj.product == PRODUCT_LIBERCLAW:
            changed_ids.add(obj.id)
    for obj in session.dirty:
        if isinstance(obj, PlanSubscription) and obj.product == PRODUCT_LIBERCLAW and _snapshot_field_changed(obj):
            changed_ids.add(obj.id)
    if not changed_ids:
        return

    for sub_id in changed_ids:
        session.execute(
            insert(PlanSubscriptionEvent).values(
                id=uuid.uuid4(),
                subscription_id=sub_id,
                event_type=PENDING_EVENT_TYPE,
                provider_event_id=None,
                metadata_json=None,
            )
        )
    session.info.setdefault(_SESSION_INFO_KEY, set()).update(changed_ids)


def schedule_pending_pushes(session: SyncSession) -> None:
    """``after_commit`` hook: schedule the HTTP push for everything ``collect_snapshot_pushes``
    flagged. Never touches ``session`` itself beyond popping the marker — each push reads
    its own fresh session, since this one may be closed by the time the task runs."""
    ids = session.info.pop(_SESSION_INFO_KEY, None)
    if not ids:
        return
    for sub_id in ids:
        _schedule_push(sub_id)


def clear_scheduled_pushes(session: SyncSession) -> None:
    """``after_rollback``/``after_soft_rollback`` hook: a rolled-back change must not still
    fire a push on this session's next successful commit. The marker row(s) inserted by the
    rolled-back flush are undone by the rollback itself; only this in-memory bookkeeping
    survives it and needs an explicit clear."""
    session.info.pop(_SESSION_INFO_KEY, None)


def _schedule_push(sub_id: uuid.UUID) -> None:
    try:
        task = asyncio.create_task(push_snapshot(sub_id))
    except RuntimeError:
        # No running event loop (e.g. a script committing outside the app's loop).
        logger.warning(f"tier_push: no running event loop to schedule push for sub {sub_id}")
        return
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)


async def _put_snapshot(snapshot: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=5.0) as client:
        return await client.put(
            f"{config.LIBERCLAW_API_URL}/internal/subscription-state",
            json=snapshot,
            headers={"x-libertai-token": config.LIBERCLAW_PUSH_SECRET},
        )


async def push_snapshot(sub_id: uuid.UUID) -> bool:
    """Re-read ``sub_id`` from a fresh session, PUT its current snapshot to LiberClaw, and
    delete only the pending marker(s) that existed at the START of this call. Never raises —
    any failure is logged and leaves the marker(s) for ``drain_pending_tier_pushes``; this may
    run detached via ``asyncio.create_task``, where an unhandled exception would otherwise
    only ever surface as "Task exception was never retrieved".

    Deleting only the markers observed at start (not "whatever matches now") matters under
    two commits racing the same row: if a second change parks its own marker while this call
    is still in flight, that marker was NOT in what we read here and must survive this call's
    cleanup — otherwise the record that a push is still owed for the second change would be
    lost until the next unrelated change happens to touch the row.

    The row read and the marker delete are two separate sessions/transactions, with the HTTP
    call strictly between them and inside neither: holding a session (and its transaction)
    open across the PUT would sit idle-in-transaction on a pooled connection for the length
    of the call.
    """
    from src.models.base import AsyncSessionLocal  # deferred: base.py imports this module

    if not push_ready():
        return False
    try:
        async with AsyncSessionLocal() as db:
            marker_ids = list(
                (
                    await db.execute(
                        select(PlanSubscriptionEvent.id).where(
                            PlanSubscriptionEvent.subscription_id == sub_id,
                            PlanSubscriptionEvent.event_type == PENDING_EVENT_TYPE,
                        )
                    )
                )
                .scalars()
                .all()
            )
            sub = (
                await db.execute(select(PlanSubscription).where(PlanSubscription.id == sub_id))
            ).scalar_one_or_none()
            if sub is None:
                logger.warning(f"tier_push: subscription {sub_id} no longer exists")
                return False
            snapshot = build_snapshot(sub)
            # Nothing to write — this just ends the read transaction before the HTTP call.
            await db.commit()

        response = await _put_snapshot(snapshot)
        response.raise_for_status()

        if marker_ids:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(PlanSubscriptionEvent).where(PlanSubscriptionEvent.id.in_(marker_ids)))
                await db.commit()
        return True
    except Exception:
        logger.warning(f"tier_push: push failed for sub {sub_id}, marker(s) left for retry", exc_info=True)
        return False


async def _bump_attempt_count(db, sub_id: uuid.UUID) -> None:
    """Track retry depth on a sub's still-pending marker(s) after a failed drain attempt, so
    a marker that never succeeds (poison) is visible in its own metadata rather than retried
    silently forever. No drop logic — the periodic pull-reconcile is the eventual net."""
    markers = (
        (
            await db.execute(
                select(PlanSubscriptionEvent).where(
                    PlanSubscriptionEvent.subscription_id == sub_id,
                    PlanSubscriptionEvent.event_type == PENDING_EVENT_TYPE,
                )
            )
        )
        .scalars()
        .all()
    )
    for marker in markers:
        attempts = (marker.metadata_json or {}).get("attempts", 0) + 1
        marker.metadata_json = {**(marker.metadata_json or {}), "attempts": attempts}
        if attempts >= DRAIN_ATTEMPT_WARN_THRESHOLD:
            logger.warning(f"tier_push: marker {marker.id} (sub {sub_id}) has failed {attempts} drain attempts")
    await db.commit()


@single_runner(LCLW_TIER_PUSH_DRAIN_LOCK_ID, skip_result=0)
async def drain_pending_tier_pushes(db) -> int:
    """Retry sweep for ``tier_push_pending`` markers a background push never cleared
    (process restart mid-push, or a push that failed). Re-reads CURRENT row state via
    ``push_snapshot`` — never a stored payload — so a row changed again since it was
    parked is pushed as it is now. Flag-gated; capped at ``DRAIN_BATCH_LIMIT`` per run so
    one large backlog can't starve the rest of the periodic loop; ``single_runner``-guarded
    so two replicas never drain the same markers concurrently.

    One row per subscription (``group_by``, oldest marker first via ``min(created_at)``) —
    a row with several markers (racing changes, see ``push_snapshot``) is still one push.
    """
    if not push_ready():
        return 0
    sub_ids = (
        (
            await db.execute(
                select(PlanSubscriptionEvent.subscription_id)
                .where(PlanSubscriptionEvent.event_type == PENDING_EVENT_TYPE)
                .group_by(PlanSubscriptionEvent.subscription_id)
                .order_by(func.min(PlanSubscriptionEvent.created_at))
                .limit(DRAIN_BATCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    pushed = 0
    for sub_id in sub_ids:
        if await push_snapshot(sub_id):
            pushed += 1
        else:
            await _bump_attempt_count(db, sub_id)
    return pushed
