"""One-shot cutover for the pending_upgrade rollout.

Run AFTER both replicas serve the new code — ``scripts/migrate.py`` runs at every replica boot
while old code still serves, and a row promoted to ``active`` is hostile to an old replica's
activation path.

Retires every never-paid checkout row (regardless of status: a ``subscription_overdue`` event
can leave one at ``overdue``), revives the one paid row a user had parked in ``upgrading``, and
gives an explicit disposition to parked rows that cannot be revived — nothing else in the
codebase moves a row out of ``upgrading`` once the revert pass is deleted.

Dry-run by default (rolls back instead of committing) — pass ``--apply`` to persist.
"""

import argparse
import asyncio
from datetime import datetime

from sqlalchemy import func, select

from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import ACTIVE_STATUSES, PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.services.payments.manager import PaymentManager
from src.services.payments.registry import payment_registry
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Statuses the null-``current_period_start`` heuristic may read as an abandoned checkout.
# ``active`` is deliberately excluded: ``_refresh_cycle_dates`` can write "active" without ever
# setting period dates (its provider read swallows failures), and a hand-created ``manual``
# grant has no periods either — both hold a real entitlement, never a checkout.
_UNPAID_CANDIDATE_STATUSES = ("pending", "pending_upgrade", "overdue", "upgrading")
# Everything this cutover can touch, unpaid or parked.
_RELEVANT_STATUSES = (*ACTIVE_STATUSES, "pending_upgrade", "upgrading")


async def cutover(db, provider, dry_run: bool = False) -> dict[str, int]:
    manager = PaymentManager(provider, db)
    counts = {"promoted": 0, "expired": 0, "stranded": 0}

    async def cancel_ok(sub: PlanSubscription) -> bool:
        # A dry run must never issue a real provider cancel — that action is irreversible and
        # is exactly what "dry run" promises not to do. Simulate success so the DB-side
        # selection logic can still be previewed; failure paths (stranded) cannot be previewed.
        if dry_run:
            logger.info(f"cutover(dry-run): would cancel sub {sub.id} at the provider")
            return True
        return await manager._cancel_on_provider(sub)

    # The null-start heuristic below only ever applies to checkout-shaped statuses. An "active"
    # row with no period dates is a live entitlement the heuristic cannot tell apart from one —
    # abort instead of guessing so it gets a human's attention.
    unexplained_active = (
        await db.execute(
            select(func.count())
            .select_from(PlanSubscription)
            .where(PlanSubscription.status == "active", PlanSubscription.current_period_start.is_(None))
        )
    ).scalar_one()
    if unexplained_active:
        raise RuntimeError(
            f"Aborting: {unexplained_active} row(s) are 'active' with no current_period_start. "
            f"These cannot be told apart from an abandoned checkout — resolve by hand, then re-run."
        )

    user_ids = (
        await db.execute(
            select(PlanSubscription.user_id).distinct().where(PlanSubscription.status.in_(_RELEVANT_STATUSES))
        )
    ).scalars().all()

    for user_id in user_ids:
        # Serializes against start_checkout/upgrade, which take this same lock before writing a
        # new row for the user — without it, FOR UPDATE below only locks rows that already
        # exist, not one inserted concurrently under the row we are about to promote.
        await manager._lock_user(user_id)
        subs = (
            await db.execute(
                select(PlanSubscription)
                .where(PlanSubscription.user_id == user_id, PlanSubscription.status.in_(_RELEVANT_STATUSES))
                .with_for_update()
            )
        ).scalars().all()

        unpaid = [s for s in subs if s.current_period_start is None and s.status in _UNPAID_CANDIDATE_STATUSES]
        failed = [s for s in unpaid if not await cancel_ok(s)]
        if failed:
            # A failed cancel on one checkout row must not cost the user their paid parked
            # subscription: touch nothing for this user, not even the rows that did cancel
            # cleanly, and leave everything for a re-run once the failure is understood.
            for sub in failed:
                logger.warning(f"cutover: sub {sub.id} (user {user_id}) would not cancel at the provider, skipping user")
            counts["stranded"] += len(failed)
            await db.rollback()
            continue

        for sub in unpaid:
            sub.status = "expired"
            db.add(PlanSubscriptionEvent(subscription_id=sub.id, event_type="expired_abandoned_checkout"))
            counts["expired"] += 1
        # Flushed before any promote write below: the one-live-subscription index is a plain
        # (non-deferrable) partial unique index, checked as each UPDATE lands — a checkout row
        # still reading "pending" when the promote's UPDATE runs collides with it even though
        # both would be valid once the whole transaction settles.
        await db.flush()

        has_live = any(s.status in ACTIVE_STATUSES for s in subs)
        parked = [s for s in subs if s.status == "upgrading" and s.current_period_start is not None]
        # DESC on current_period_end, NULLS LAST, tie-broken by created_at DESC then id DESC.
        parked.sort(key=lambda s: (s.current_period_end or datetime.min, s.created_at, s.id), reverse=True)
        for i, sub in enumerate(parked):
            if i == 0 and not has_live:
                sub.status = "active"
                db.add(PlanSubscriptionEvent(subscription_id=sub.id, event_type="upgrade_abandoned_reverted"))
                counts["promoted"] += 1
                continue
            if not await cancel_ok(sub):
                counts["stranded"] += 1
                logger.warning(f"cutover: parked sub {sub.id} (user {user_id}) would not cancel, left in upgrading")
                continue
            sub.status = "cancelled"
            db.add(PlanSubscriptionEvent(subscription_id=sub.id, event_type="cancelled_for_upgrade"))
            await manager._credit_unused_remainder(sub)

        # Committed per user: an abort partway through must not roll back users already
        # resolved, whose provider cancels already happened and cannot be undone. A dry run
        # rolls back instead, so nothing it touched is ever persisted.
        if dry_run:
            await db.rollback()
        else:
            await db.commit()

    logger.info(f"cutover: {counts}")
    return counts


async def main(apply: bool) -> None:
    async with AsyncSessionLocal() as db:
        counts = await cutover(db, payment_registry.get("revolut"), dry_run=not apply)
    print(("APPLIED " if apply else "DRY RUN ") + str(counts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry run).")
    asyncio.run(main(parser.parse_args().apply))
