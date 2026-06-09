"""PaymentManager state machine: top-ups + subscriptions, with a fake provider."""

import uuid

import pytest
from sqlalchemy import func, select

from src.interfaces.credits import CreditTransactionStatus
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
    PaymentProviderKind,
    ProviderDescriptor,
    SubscriptionInfo,
)
from src.services.payments.manager import PaymentManager, _topup_tx_hash


class FakeProvider(PaymentProvider):
    """In-memory provider supporting both top-ups and subscriptions."""

    def __init__(self):
        self.order_seq = 0
        self.sub_seq = 0
        self.cancelled: list[str] = []

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="fake",
            kind=PaymentProviderKind.fiat,
            label="Fake",
            capabilities=[PaymentCapability.topup, PaymentCapability.subscription],
            currencies=["USD"],
        )

    async def create_topup(self, *, amount, currency, redirect_url, user_email=None, metadata=None):
        self.order_seq += 1
        return CheckoutResult(checkout_url="http://pay/topup", order_id=f"ord_{self.order_seq}")

    async def create_subscription(self, *, user_email, tier, currency, redirect_url, provider_customer_id=None):
        self.sub_seq += 1
        return CheckoutResult(
            checkout_url="http://pay/sub",
            provider_subscription_id=f"psub_{self.sub_seq}",
            provider_customer_id=provider_customer_id or "cust_1",
            order_id=f"setup_{self.sub_seq}",
        )

    async def cancel_subscription(self, provider_subscription_id: str) -> None:
        self.cancelled.append(provider_subscription_id)

    async def get_subscription(self, provider_subscription_id: str) -> SubscriptionInfo:
        return SubscriptionInfo(
            provider_subscription_id=provider_subscription_id,
            state="active",
            current_cycle_start="2026-06-01T00:00:00+00:00",
            current_cycle_end="2026-07-01T00:00:00+00:00",
        )

    async def get_order(self, order_id: str) -> dict:
        return {}


async def _make_user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", email_verified=True)
    db.add(user)
    await db.flush()
    return user


async def _balance(db, user_id) -> float:
    total = (
        await db.execute(
            select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.is_active == True,  # noqa: E712
                CreditTransaction.status == CreditTransactionStatus.completed,
            )
        )
    ).scalar()
    return float(total or 0.0)


@pytest.mark.asyncio
async def test_topup_completes_once_and_dedups(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)

    result = await mgr.start_topup(user, amount=10.0, redirect_url="http://x")
    assert result.checkout_url
    # Pending -> not yet spendable.
    assert await _balance(db, user.id) == 0.0

    event = PaymentEvent(
        provider="fake", type=PaymentEventType.order_completed,
        provider_event_id="ORDER_COMPLETED:ord_1", order_id="ord_1",
    )
    await mgr.handle_event(event)
    assert await _balance(db, user.id) == 10.0

    # Replay the same completion — no double credit.
    await mgr.handle_event(event)
    assert await _balance(db, user.id) == 10.0


@pytest.mark.asyncio
async def test_topup_failure_voids_pending(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_topup(user, amount=5.0, redirect_url="http://x")

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                     provider_event_id="ORDER_FAILED:ord_1", order_id="ord_1")
    )
    tx = (
        await db.execute(
            select(CreditTransaction).where(
                CreditTransaction.transaction_hash == _topup_tx_hash("fake", "ord_1")
            )
        )
    ).scalar_one()
    assert tx.status == CreditTransactionStatus.error
    assert await _balance(db, user.id) == 0.0


@pytest.mark.asyncio
async def test_subscribe_activates_tier(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)

    await mgr.start_checkout(user, tier="plus", redirect_url="http://x")
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.status == "pending"
    assert await mgr.current_tier(user.id) == "free"  # not active yet

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1")
    )
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.status == "active"
    assert sub.current_period_end is not None
    assert await mgr.current_tier(user.id) == "plus"


@pytest.mark.asyncio
async def test_subscription_event_dedup(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(user, tier="go", redirect_url="http://x")

    event = PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                         provider_event_id="ORDER_COMPLETED:setup_1",
                         provider_subscription_id="psub_1", order_id="setup_1")
    await mgr.handle_event(event)
    await mgr.handle_event(event)  # replay

    activated = (
        await db.execute(
            select(func.count()).select_from(PlanSubscriptionEvent).where(
                PlanSubscriptionEvent.event_type == "activated"
            )
        )
    ).scalar()
    assert activated == 1


@pytest.mark.asyncio
async def test_upgrade_parks_then_cancels_old(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    # Active go sub.
    await mgr.start_checkout(user, tier="go", redirect_url="http://x")
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1")
    )
    assert await mgr.current_tier(user.id) == "go"

    # Upgrade to plus -> parks old as "upgrading", new pending sub created.
    await mgr.upgrade(user, new_tier="plus", redirect_url="http://x")
    parked = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.user_id == user.id, PlanSubscription.status == "upgrading"
            )
        )
    ).scalar_one()
    assert parked.tier == "go"

    # Pay the new sub -> activates plus, old gets cancelled.
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_2",
                     provider_subscription_id="psub_2", order_id="setup_2")
    )
    assert await mgr.current_tier(user.id) == "plus"
    assert "psub_1" in provider.cancelled
    refreshed_old = await db.get(PlanSubscription, parked.id)
    assert refreshed_old.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_sets_period_end_flag(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x")
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1")
    )

    res = await mgr.cancel(user)
    assert "end of billing period" in res["message"]
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.cancel_at_period_end is True
    assert sub.status == "active"  # still active until period end
    assert "psub_1" in provider.cancelled
