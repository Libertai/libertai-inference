"""Credit-billed subscription service.

Subscriptions paid by deducting prepaid credits (provider id "credits").
Renewals are handled by ``process_renewals`` called from a cron job.
"""

from __future__ import annotations

from datetime import datetime

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.services.credit import CreditService
from src.subscription_tiers import (
    DEFAULT_TIER,
    PAID_TIERS,
    get_tier,
    is_downgrade,
    is_upgrade,
)

CREDITS_PROVIDER = "credits"


class CreditSubscriptionService:
    # ------------------------------------------------------------------ helpers

    @staticmethod
    def monthly_price(tier: str) -> float:
        return get_tier(tier).price_cents / 100

    @staticmethod
    async def _active_sub(db: AsyncSession, user_id) -> PlanSubscription | None:
        """Load the user's active credits subscription with a row-level lock."""
        stmt = (
            select(PlanSubscription)
            .where(
                PlanSubscription.user_id == user_id,
                PlanSubscription.provider == CREDITS_PROVIDER,
                PlanSubscription.status == "active",
            )
            .with_for_update()
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def _log(
        db: AsyncSession,
        sub: PlanSubscription,
        event_type: str,
        metadata: dict | None = None,
    ) -> None:
        db.add(
            PlanSubscriptionEvent(
                subscription_id=sub.id,
                event_type=event_type,
                provider_event_id=None,
                metadata_json=metadata,
            )
        )

    # ------------------------------------------------------------------ public API

    @staticmethod
    async def subscribe(db: AsyncSession, user, tier: str) -> PlanSubscription:
        """Create and immediately activate a credits subscription for *user*."""
        if tier not in PAID_TIERS:
            raise ValueError("Unknown or non-paid tier")

        # Enforce one-active-sub-per-user (mirrors the partial unique index).
        existing = (
            await db.execute(
                select(PlanSubscription).where(
                    PlanSubscription.user_id == user.id,
                    PlanSubscription.status.in_(["pending", "active", "overdue"]),
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise ValueError("Already subscribed")

        price = CreditSubscriptionService.monthly_price(tier)
        if await CreditService.get_balance(user.id) < price:
            raise ValueError("Insufficient credits — top up first")

        ok = await CreditService.use_credits(user.id, price)
        if not ok:
            raise ValueError("Insufficient credits — top up first")

        now = datetime.now()
        sub = PlanSubscription(
            user_id=user.id,
            tier=tier,
            status="active",
            provider=CREDITS_PROVIDER,
            currency="USD",
            current_period_start=now,
            current_period_end=now + relativedelta(months=1),
            cancel_at_period_end=False,
        )
        db.add(sub)
        await db.flush()
        await CreditSubscriptionService._log(db, sub, "activated")
        return sub

    @staticmethod
    async def upgrade(db: AsyncSession, user, new_tier: str) -> PlanSubscription:
        """Immediately upgrade to *new_tier*, charging only the prorated difference."""
        sub = await CreditSubscriptionService._active_sub(db, user.id)
        if sub is None:
            raise ValueError("No active credits subscription")
        if not is_upgrade(sub.tier, new_tier):
            raise ValueError("Not an upgrade")

        now = datetime.now()
        span = (sub.current_period_end - sub.current_period_start).total_seconds()
        remaining = max(0.0, (sub.current_period_end - now).total_seconds())
        frac = remaining / span if span > 0 else 1.0
        charge = max(
            0.0,
            (CreditSubscriptionService.monthly_price(new_tier) - CreditSubscriptionService.monthly_price(sub.tier))
            * frac,
        )

        if charge > 0:
            if await CreditService.get_balance(user.id) < charge:
                raise ValueError("Insufficient credits — top up first")
            ok = await CreditService.use_credits(user.id, charge)
            if not ok:
                raise ValueError("Insufficient credits — top up first")

        sub.tier = new_tier
        sub.pending_tier = None
        await db.flush()
        await CreditSubscriptionService._log(db, sub, "upgraded", metadata={"prorated_charge": charge})
        return sub

    @staticmethod
    async def request_downgrade(db: AsyncSession, user, new_tier: str) -> dict:
        """Schedule a downgrade to take effect at the end of the current period."""
        sub = await CreditSubscriptionService._active_sub(db, user.id)
        if sub is None:
            raise ValueError("No active credits subscription")
        if not is_downgrade(sub.tier, new_tier):
            raise ValueError("Not a downgrade")

        sub.pending_tier = new_tier
        if new_tier == DEFAULT_TIER:
            sub.cancel_at_period_end = True
        await db.flush()
        await CreditSubscriptionService._log(db, sub, "downgrade_requested", metadata={"new_tier": new_tier})
        return {"new_tier": new_tier, "effective_date": sub.current_period_end}

    @staticmethod
    async def cancel(db: AsyncSession, user) -> dict:
        """Mark subscription to cancel at period end."""
        sub = await CreditSubscriptionService._active_sub(db, user.id)
        if sub is None:
            raise ValueError("No active credits subscription")

        sub.cancel_at_period_end = True
        await db.flush()
        await CreditSubscriptionService._log(db, sub, "cancel_requested")
        return {
            "message": "Subscription will be cancelled at the end of the billing period",
            "effective_date": sub.current_period_end,
        }

    @staticmethod
    async def process_renewals(db: AsyncSession, now: datetime | None = None) -> int:
        """Renew (or expire) all credits subscriptions whose period has ended.

        Caller is responsible for committing the session.
        Returns the number of subscriptions processed.
        """
        now = now or datetime.now()

        stmt = (
            select(PlanSubscription)
            .where(
                PlanSubscription.provider == CREDITS_PROVIDER,
                PlanSubscription.status == "active",
                PlanSubscription.current_period_end <= now,
            )
            .with_for_update()
        )
        subs = (await db.execute(stmt)).scalars().all()

        count = 0
        for sub in subs:
            # Cancelled or downgraded-to-free: expire without charging.
            if sub.cancel_at_period_end or sub.pending_tier == DEFAULT_TIER:
                sub.status = "expired"
                await CreditSubscriptionService._log(db, sub, "expired")
                count += 1
                continue

            target = sub.pending_tier or sub.tier
            price = CreditSubscriptionService.monthly_price(target)

            if await CreditService.get_balance(sub.user_id) < price:
                sub.status = "expired"
                await CreditSubscriptionService._log(db, sub, "expired_insufficient_credits")
                count += 1
                continue

            await CreditService.use_credits(sub.user_id, price)
            sub.tier = target
            sub.pending_tier = None
            sub.current_period_start = now
            sub.current_period_end = now + relativedelta(months=1)
            await CreditSubscriptionService._log(db, sub, "renewed")
            count += 1

        await db.flush()
        return count
