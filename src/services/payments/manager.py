"""Provider-agnostic payment orchestrator.

Owns the business logic for **top-ups** (one-off prepaid credit purchases) and
**subscriptions** (recurring tiers), driven by normalized :class:`PaymentEvent`s.
It never references a concrete provider — it is handed a :class:`PaymentProvider`
and an :class:`AsyncSession`, and speaks only the abstraction.

Ported/condensed from the liberclaw subscription manager; the remote tier-sync
(``change_user_tier`` HTTP call) is dropped because the tier now lives locally on
the ``plan_subscriptions`` row and is read directly by the entitlement service.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.services.payments.base import (
    CheckoutResult,
    PaymentCapability,
    PaymentEvent,
    PaymentEventType,
    PaymentProvider,
    UnsupportedCapability,
)
from src.subscription_tiers import (
    DEFAULT_TIER,
    PAID_TIERS,
    is_downgrade,
    is_upgrade,
)

logger = logging.getLogger(__name__)

# A top-up credit transaction is keyed by this hash so webhook replays dedup and
# the pending row created at checkout time can be completed on confirmation.
TOPUP_EXT_REF_PREFIX = "topup:"


def _topup_tx_hash(provider_id: str, order_id: str) -> str:
    return f"{provider_id}:{order_id}"


class PaymentManager:
    def __init__(self, provider: PaymentProvider, db: AsyncSession):
        self.provider = provider
        self.db = db

    # ------------------------------------------------------------------ helpers
    async def _active_subscription(self, user_id: uuid.UUID, lock: bool = True) -> PlanSubscription | None:
        stmt = select(PlanSubscription).where(
            PlanSubscription.user_id == user_id,
            PlanSubscription.status.in_(["pending", "active", "overdue"]),
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def current_tier(self, user_id: uuid.UUID) -> str:
        sub = await self._active_subscription(user_id, lock=False)
        if sub and sub.status == "active":
            return sub.tier
        return DEFAULT_TIER

    async def _log_event(
        self,
        subscription: PlanSubscription,
        event_type: str,
        provider_event_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.db.add(
            PlanSubscriptionEvent(
                subscription_id=subscription.id,
                event_type=event_type,
                provider_event_id=provider_event_id,
                metadata_json=metadata,
            )
        )

    # ------------------------------------------------------------------ top-ups
    async def start_topup(
        self,
        user: User,
        redirect_url: str,
        *,
        usd_credits: float,
        charge_amount: float,
        charge_currency: str,
    ) -> CheckoutResult:
        """Open a hosted checkout for a one-off prepaid credit purchase.

        The charge and the credit are decoupled: non-EU users pay arbitrary USD
        1:1 (``usd_credits == charge_amount``), while EU users buy a fixed pack —
        ``charge_amount`` is the gross EUR price (VAT-inclusive, VAT handled at
        the Revolut merchant level) and ``usd_credits`` is the fixed USD value
        credited. The pending row always records ``usd_credits`` because the
        prepaid balance is USD-denominated and webhook settlement completes the
        recorded amount as-is.
        """
        if not self.provider.supports(PaymentCapability.topup):
            raise UnsupportedCapability(f"{self.provider.id} does not support top-ups")
        if usd_credits <= 0:
            raise ValueError("Top-up credit amount must be positive")
        if charge_amount <= 0:
            raise ValueError("Top-up charge amount must be positive")

        result = await self.provider.create_topup(
            amount=charge_amount,
            currency=charge_currency,
            redirect_url=redirect_url,
            user_email=user.email,
            metadata={"ext_ref": f"{TOPUP_EXT_REF_PREFIX}{user.id}"},
        )

        # Record the credits as pending now; they become spendable when the
        # provider confirms payment (ORDER_COMPLETED). Pending rows don't count
        # toward the balance (balance filters on status == completed).
        if result.order_id:
            self.db.add(
                CreditTransaction(
                    user_id=user.id,
                    amount=usd_credits,
                    amount_left=usd_credits,
                    provider=CreditTransactionProvider.revolut,
                    transaction_hash=_topup_tx_hash(self.provider.id, result.order_id),
                    status=CreditTransactionStatus.pending,
                    is_active=True,
                )
            )
            await self.db.flush()
        return result

    async def _settle_topup(self, event: PaymentEvent) -> bool:
        """Complete/fail a pending top-up. Returns True if this event was a top-up."""
        if not event.order_id:
            return False
        tx = (
            await self.db.execute(
                select(CreditTransaction)
                .where(CreditTransaction.transaction_hash == _topup_tx_hash(event.provider, event.order_id))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if tx is None:
            return False  # not a (locally-recorded) top-up — let the subscription path try

        if event.type == PaymentEventType.order_completed:
            if tx.status != CreditTransactionStatus.completed:
                tx.status = CreditTransactionStatus.completed
                logger.info(f"Top-up {tx.transaction_hash} completed ({tx.amount} credits)")
            else:
                logger.info(f"Top-up {tx.transaction_hash} already completed, skipping")
        elif event.type == PaymentEventType.order_failed:
            tx.status = CreditTransactionStatus.error
            tx.is_active = False
            tx.amount_left = 0
        await self.db.flush()
        return True

    # ------------------------------------------------------------------ subscriptions
    async def start_checkout(self, user: User, tier: str, redirect_url: str, currency: str) -> CheckoutResult:
        if tier not in PAID_TIERS:
            raise ValueError(f"Invalid paid tier: {tier}")
        if not self.provider.supports(PaymentCapability.subscription):
            raise UnsupportedCapability(f"{self.provider.id} does not support subscriptions")
        if not user.email:
            raise ValueError("User must have an email to subscribe")

        existing = await self._active_subscription(user.id)
        if existing:
            if existing.status == "pending" and existing.current_period_end is None:
                # Abandoned checkout — retire it so the one-active-sub index frees up.
                await self._cancel_on_provider(existing)
                existing.status = "expired"
                await self._log_event(existing, "expired_abandoned_checkout")
                await self.db.flush()
            else:
                raise ValueError("User already has an active subscription")

        prev_customer_id = (
            await self.db.execute(
                select(PlanSubscription.provider_customer_id)
                .where(
                    PlanSubscription.user_id == user.id,
                    PlanSubscription.provider_customer_id.isnot(None),
                )
                .order_by(PlanSubscription.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        result = await self.provider.create_subscription(
            user_email=user.email,
            tier=tier,
            currency=currency,
            redirect_url=redirect_url,
            provider_customer_id=prev_customer_id,
        )

        sub = PlanSubscription(
            user_id=user.id,
            tier=tier,
            status="pending",
            provider=self.provider.id,
            provider_subscription_id=result.provider_subscription_id,
            provider_customer_id=result.provider_customer_id,
            # Locked at checkout time: renewals bill through the provider's
            # currency-specific plan, so the currency never changes mid-life.
            currency=currency,
        )
        self.db.add(sub)
        try:
            await self.db.flush()
        except IntegrityError:
            raise ValueError("User already has an active subscription")
        await self._log_event(sub, "created", metadata={"tier": tier})
        return result

    async def upgrade(self, user: User, new_tier: str, redirect_url: str, currency: str) -> CheckoutResult:
        if new_tier not in PAID_TIERS:
            raise ValueError(f"Invalid tier: {new_tier}")
        current = await self.current_tier(user.id)
        if not is_upgrade(current, new_tier):
            raise ValueError(f"Cannot upgrade from {current} to {new_tier}")

        existing = await self._active_subscription(user.id)
        if existing:
            # Park the old sub — "upgrading" is excluded from the one-active-sub index.
            existing.status = "upgrading"
            await self._log_event(existing, "upgrading", metadata={"new_tier": new_tier})
            await self.db.flush()
        return await self.start_checkout(user, new_tier, redirect_url, currency)

    async def cancel(self, user: User) -> dict:
        sub = await self._active_subscription(user.id)
        if not sub:
            raise ValueError("No active subscription")
        sub.cancel_at_period_end = True
        await self._cancel_on_provider(sub)
        await self._log_event(sub, "cancel_requested")
        await self.db.flush()
        return {
            "message": "Subscription will be cancelled at end of billing period",
            "effective_date": sub.current_period_end.isoformat() if sub.current_period_end else None,
        }

    async def request_downgrade(self, user: User, new_tier: str) -> dict:
        if new_tier not in PAID_TIERS and new_tier != DEFAULT_TIER:
            raise ValueError(f"Invalid tier: {new_tier}")
        current = await self.current_tier(user.id)
        if not is_downgrade(current, new_tier):
            raise ValueError(f"Cannot downgrade from {current} to {new_tier}")
        sub = await self._active_subscription(user.id)
        if not sub:
            raise ValueError("No active subscription")
        sub.pending_tier = new_tier
        sub.cancel_at_period_end = True
        if new_tier == DEFAULT_TIER:
            await self._cancel_on_provider(sub)
        await self._log_event(sub, "downgrade_requested", metadata={"new_tier": new_tier})
        await self.db.flush()
        return {
            "effective_date": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "new_tier": new_tier,
        }

    async def _cancel_on_provider(self, sub: PlanSubscription) -> None:
        if sub.provider == "manual" or not sub.provider_subscription_id:
            return
        try:
            await self.provider.cancel_subscription(sub.provider_subscription_id)
        except Exception:
            logger.warning(f"Failed to cancel sub {sub.id} on provider", exc_info=True)

    async def _cancel_upgrading_subs(self, user_id: uuid.UUID, exclude_sub_id: uuid.UUID) -> None:
        result = await self.db.execute(
            select(PlanSubscription).where(
                PlanSubscription.user_id == user_id,
                PlanSubscription.status == "upgrading",
                PlanSubscription.id != exclude_sub_id,
            )
        )
        for old_sub in result.scalars().all():
            await self._cancel_on_provider(old_sub)
            old_sub.status = "cancelled"
            await self._log_event(old_sub, "cancelled_for_upgrade")

    # ------------------------------------------------------------------ webhook dispatch
    async def handle_event(self, event: PaymentEvent) -> None:
        # Top-ups settle first (local pending row keyed by order id).
        if event.type in (PaymentEventType.order_completed, PaymentEventType.order_failed):
            if await self._settle_topup(event):
                return

        sub = await self._resolve_subscription(event)
        if not sub:
            logger.warning(f"No subscription found for payment event: {event}")
            return

        # Dedup subscription events.
        if event.provider_event_id:
            existing = (
                await self.db.execute(
                    select(PlanSubscriptionEvent.id).where(
                        PlanSubscriptionEvent.provider_event_id == event.provider_event_id
                    )
                )
            ).scalar_one_or_none()
            if existing:
                logger.info(f"Duplicate event {event.provider_event_id}, skipping")
                return

        user = (await self.db.execute(select(User).where(User.id == sub.user_id))).scalar_one()

        if event.type == PaymentEventType.order_completed:
            sub.status = "active"
            await self._refresh_cycle_dates(sub)
            await self._log_event(sub, "activated", event.provider_event_id, event.metadata)
            await self._cancel_upgrading_subs(user.id, exclude_sub_id=sub.id)
        elif event.type == PaymentEventType.order_failed:
            sub.status = "overdue"
            await self._log_event(sub, "payment_failed", event.provider_event_id, event.metadata)
        elif event.type == PaymentEventType.subscription_overdue:
            sub.status = "overdue"
            await self._log_event(sub, "overdue", event.provider_event_id, event.metadata)
        elif event.type == PaymentEventType.subscription_cancelled:
            sub.status = "cancelled"
            await self._log_event(sub, "cancelled", event.provider_event_id, event.metadata)
        elif event.type == PaymentEventType.subscription_initiated:
            await self._log_event(sub, "initiated", event.provider_event_id, event.metadata)
        elif event.type == PaymentEventType.subscription_finished:
            sub.status = "expired"
            await self._log_event(sub, "finished", event.provider_event_id, event.metadata)

        await self.db.flush()

    async def _resolve_subscription(self, event: PaymentEvent) -> PlanSubscription | None:
        if event.provider_subscription_id:
            sub = (
                await self.db.execute(
                    select(PlanSubscription)
                    .where(PlanSubscription.provider_subscription_id == event.provider_subscription_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if sub:
                return sub

        if event.order_id:
            try:
                order = await self.provider.get_order(event.order_id)
                channel = order.get("channel_data") or {}
                rev_sub_id = channel.get("subscription_id")
                if rev_sub_id:
                    return (
                        await self.db.execute(
                            select(PlanSubscription)
                            .where(PlanSubscription.provider_subscription_id == rev_sub_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
            except Exception:
                logger.warning(f"Failed to resolve order {event.order_id} to subscription", exc_info=True)
        return None

    async def _refresh_cycle_dates(self, sub: PlanSubscription) -> None:
        if not sub.provider_subscription_id:
            return
        try:
            info = await self.provider.get_subscription(sub.provider_subscription_id)
            if info.current_cycle_start:
                sub.current_period_start = datetime.fromisoformat(info.current_cycle_start)
            if info.current_cycle_end:
                sub.current_period_end = datetime.fromisoformat(info.current_cycle_end)
        except Exception:
            logger.warning("Failed to fetch cycle dates", exc_info=True)

    # ------------------------------------------------------------------ periodic
    async def check_expirations(self) -> int:
        """Expire subscriptions past their period end (24h grace to avoid racing webhooks).

        Naive ``datetime.now()`` on purpose: the TIMESTAMP columns are naive and every
        other period computation (credit_subscription, entitlement) uses naive local
        time — mixing in an aware UTC cutoff here skewed the comparison on non-UTC hosts.
        """
        cutoff = datetime.now() - timedelta(hours=24)
        result = await self.db.execute(
            select(PlanSubscription)
            .where(
                PlanSubscription.status.in_(["active", "overdue"]),
                PlanSubscription.current_period_end < cutoff,
                (PlanSubscription.cancel_at_period_end == True)  # noqa: E712
                | (PlanSubscription.is_trial == True),  # noqa: E712
            )
            .with_for_update()
        )
        count = 0
        for sub in result.scalars().all():
            sub.status = "expired"
            await self._log_event(sub, "expired", metadata={"new_tier": sub.pending_tier or DEFAULT_TIER})
            count += 1

        # Abandoned upgrades: ``upgrade()`` parks the old sub as "upgrading" until the
        # new checkout's ORDER_COMPLETED cancels it. If the user never pays, the parked
        # sub would stay "upgrading" forever — no entitlement, while the provider keeps
        # billing it. Revert to "active" after 1h (no provider call: the old plan was
        # never cancelled). Skip users who still have a live row (the new checkout's
        # pending sub): reverting would violate the one-active-sub index, and that
        # row's completion cancels the parked sub anyway.
        stale_cutoff = datetime.now() - timedelta(hours=1)
        stale = await self.db.execute(
            select(PlanSubscription)
            .where(
                PlanSubscription.status == "upgrading",
                PlanSubscription.updated_at < stale_cutoff,
            )
            .with_for_update()
        )
        for sub in stale.scalars().all():
            if await self._active_subscription(sub.user_id, lock=False):
                continue
            sub.status = "active"
            await self._log_event(sub, "upgrade_abandoned_reverted")
            count += 1

        if count:
            await self.db.flush()
        return count
