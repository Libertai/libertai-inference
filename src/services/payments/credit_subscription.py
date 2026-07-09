"""Credit-billed subscription service.

Subscriptions paid by deducting prepaid credits (provider id "credits").
Renewals are handled by ``process_renewals`` called from a cron job.
"""

from __future__ import annotations

from datetime import datetime

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

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
            raise ValueError("You already have an active subscription")

        price = CreditSubscriptionService.monthly_price(tier)
        if await CreditService.get_balance(user.id, db=db) < price:
            raise ValueError("Insufficient credits — top up first")

        ok = await CreditService.use_credits(user.id, price, db=db)
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
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent subscribe slipped past the select above; the partial
            # unique index (one live sub per user) is the source of truth.
            raise ValueError("You already have an active subscription")
        await CreditSubscriptionService._log(db, sub, "activated")
        return sub

    @staticmethod
    async def upgrade(db: AsyncSession, user, new_tier: str) -> PlanSubscription:
        """Immediately upgrade to *new_tier*, charging only the prorated difference."""
        sub = await CreditSubscriptionService._active_sub(db, user.id)
        if sub is None:
            raise ValueError("No active credits subscription")

        now = datetime.now()
        # A lapsed period (the hourly cron hasn't renewed yet) would prorate to
        # remaining=0 -> a free upgrade. Renew inline first so the proration runs
        # against a fresh period; an expiry outcome (cancelled / unfunded) stands.
        if sub.current_period_end is not None and sub.current_period_end <= now:
            await CreditSubscriptionService._process_one_renewal(db, sub, now)
            if sub.status != "active":
                raise ValueError("Subscription expired at period end — subscribe again")

        if not is_upgrade(sub.tier, new_tier):
            raise ValueError("Not an upgrade")
        if sub.current_period_end is None or sub.current_period_start is None:
            raise ValueError("Subscription has no active billing period")
        span = (sub.current_period_end - sub.current_period_start).total_seconds()
        remaining = max(0.0, (sub.current_period_end - now).total_seconds())
        frac = remaining / span if span > 0 else 1.0
        charge = max(
            0.0,
            (CreditSubscriptionService.monthly_price(new_tier) - CreditSubscriptionService.monthly_price(sub.tier))
            * frac,
        )

        if charge > 0:
            if await CreditService.get_balance(user.id, db=db) < charge:
                raise ValueError("Insufficient credits — top up first")
            ok = await CreditService.use_credits(user.id, charge, db=db)
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
        if new_tier != DEFAULT_TIER and new_tier not in PAID_TIERS:
            raise ValueError("Unknown tier")
        sub = await CreditSubscriptionService._active_sub(db, user.id)
        if sub is None:
            raise ValueError("No active credits subscription")
        if not is_downgrade(sub.tier, new_tier):
            raise ValueError("Not a downgrade")

        sub.pending_tier = new_tier
        # Re-requesting a downgrade replaces any earlier one: only a downgrade to
        # free cancels; a paid target clears a previously scheduled cancellation.
        sub.cancel_at_period_end = new_tier == DEFAULT_TIER
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
        sub.pending_tier = DEFAULT_TIER  # cancel == downgrade to free (drives the plans UI)
        await db.flush()
        await CreditSubscriptionService._log(db, sub, "cancel_requested")
        return {
            "message": "Subscription will be cancelled at the end of the billing period",
            "effective_date": sub.current_period_end,
        }

    @staticmethod
    async def resume(db: AsyncSession, user) -> dict:
        """Undo a scheduled cancellation or downgrade before the period ends (local-only:
        credits subscriptions have no provider side)."""
        sub = await CreditSubscriptionService._active_sub(db, user.id)
        if sub is None:
            raise ValueError("No active credits subscription")
        if not sub.cancel_at_period_end and not sub.pending_tier:
            raise ValueError("Nothing to resume")
        sub.cancel_at_period_end = False
        sub.pending_tier = None
        await db.flush()
        await CreditSubscriptionService._log(db, sub, "resumed")
        return {"message": "Your subscription will continue", "tier": sub.tier}

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
            # One bad subscription must not block the whole batch.
            try:
                count += await CreditSubscriptionService._process_one_renewal(db, sub, now)
            except Exception:
                logger.error(f"Failed to process renewal for subscription {sub.id}", exc_info=True)

        await db.flush()
        return count

    @staticmethod
    async def _process_one_renewal(db: AsyncSession, sub: PlanSubscription, now: datetime) -> int:
        # Cancelled or downgraded-to-free: expire without charging.
        if sub.cancel_at_period_end or sub.pending_tier == DEFAULT_TIER:
            sub.status = "expired"
            await CreditSubscriptionService._log(db, sub, "expired")
            return 1

        target = sub.pending_tier or sub.tier
        price = CreditSubscriptionService.monthly_price(target)

        if await CreditService.get_balance(sub.user_id, db=db) < price:
            sub.status = "expired"
            await CreditSubscriptionService._log(db, sub, "expired_insufficient_credits")
            return 1

        ok = await CreditService.use_credits(sub.user_id, price, db=db)
        if not ok:
            sub.status = "expired"
            await CreditSubscriptionService._log(db, sub, "expired_insufficient_credits")
            return 1

        # Anchor the new period at the previous period's end (not the cron run time)
        # so cycles don't drift later by up to the cron interval on every renewal.
        # If the cron was down for more than a full cycle, re-anchor at now instead
        # of back-billing an already-elapsed period.
        period_start = sub.current_period_end or now
        period_end = period_start + relativedelta(months=1)
        if period_end <= now:
            period_start, period_end = now, now + relativedelta(months=1)

        sub.tier = target
        sub.pending_tier = None
        sub.current_period_start = period_start
        sub.current_period_end = period_end
        await CreditSubscriptionService._log(db, sub, "renewed")
        return 1
