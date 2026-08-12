"""One-shot cutover for the pending_upgrade rollout.

Run AFTER both replicas serve the new code — ``scripts/migrate.py`` runs at every replica boot
while old code still serves, and a row promoted to ``active`` is hostile to an old replica's
activation path.

Retires every never-paid checkout row (regardless of status: a ``subscription_overdue`` event
can leave one at ``overdue``), revives the one paid row a user had parked in ``upgrading``, and
gives an explicit disposition to parked rows that cannot be revived — nothing else in the
codebase moves a row out of ``upgrading`` once the revert pass is deleted.
"""

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select

from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import ACTIVE_STATUSES, PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.services.payments.manager import PaymentManager
from src.services.payments.registry import payment_registry
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Rows this cutover can touch. Anything already terminal (expired/cancelled/finished) is left
# alone. ``pending_upgrade``/``upgrading`` are the new/old parked-checkout shapes respectively.
_RELEVANT_STATUSES = (*ACTIVE_STATUSES, "pending_upgrade", "upgrading")


async def cutover(db, provider) -> dict[str, int]:
    manager = PaymentManager(provider, db)
    counts = {"promoted": 0, "expired": 0, "stranded": 0}

    rows = (
        await db.execute(
            select(PlanSubscription)
            .where(PlanSubscription.status.in_(_RELEVANT_STATUSES))
            .with_for_update()
        )
    ).scalars().all()
    by_user: dict[uuid.UUID, list[PlanSubscription]] = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)

    for subs in by_user.values():
        # Never-paid rows, regardless of status — a failed provider cancel leaves the row as-is
        # (the write is gated on the cancel) rather than expiring it locally while it stays
        # live and payable at the provider.
        unpaid = [s for s in subs if s.current_period_start is None]
        for sub in unpaid:
            if not await manager._cancel_on_provider(sub):
                continue
            sub.status = "expired"
            db.add(PlanSubscriptionEvent(subscription_id=sub.id, event_type="expired_abandoned_checkout"))
            counts["expired"] += 1
        # Flushed before any promote write below: the one-live-subscription index is a plain
        # (non-deferrable) partial unique index, checked as each UPDATE lands — a checkout row
        # still reading "pending" when the promote's UPDATE runs collides with it even though
        # both would be valid once the whole transaction settles.
        await db.flush()

        # Computed after the unpaid pass so a row that failed to cancel (and so is still
        # sitting in an ACTIVE_STATUSES-shaped status) correctly counts as live and blocks the
        # promote below — promoting anyway would collide with it on the one-live-subscription
        # index at flush.
        has_live = any(s.status in ACTIVE_STATUSES for s in subs)

        parked = [s for s in subs if s.status == "upgrading" and s.current_period_start is not None]
        # DESC on current_period_end, NULLS LAST, tie-broken by created_at DESC.
        parked.sort(key=lambda s: (s.current_period_end or datetime.min, s.created_at), reverse=True)
        for i, sub in enumerate(parked):
            if i == 0 and not has_live:
                sub.status = "active"
                db.add(PlanSubscriptionEvent(subscription_id=sub.id, event_type="upgrade_abandoned_reverted"))
                counts["promoted"] += 1
                continue
            if not await manager._cancel_on_provider(sub):
                counts["stranded"] += 1
                continue
            sub.status = "cancelled"
            db.add(PlanSubscriptionEvent(subscription_id=sub.id, event_type="cancelled_for_upgrade"))

        await db.flush()

    logger.info(f"cutover: {counts}")
    return counts


async def main() -> None:
    async with AsyncSessionLocal() as db:
        counts = await cutover(db, payment_registry.get("revolut"))
        await db.commit()
    print(counts)


if __name__ == "__main__":
    asyncio.run(main())
