"""Entitlement windows start empty when a plan comes into entitlement or is upgraded,
and keep running across a renewal of a live plan."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.credit_transaction import CreditTransaction
from src.models.entitlement_window import EntitlementWindow
from src.models.user import User
from src.services.entitlement import WINDOW_5H, WINDOW_WEEKLY, reset_windows
from src.services.payments.base import PaymentEvent, PaymentEventType
from src.services.payments.credit_subscription import CreditSubscriptionService
from src.services.payments.manager import PaymentManager
from src.services.payments.owner import Owner
from tests.test_payment_manager import FakeProvider


async def _user(db) -> User:
    u = User(email=f"{uuid.uuid4().hex}@example.com")
    db.add(u)
    await db.flush()
    return u


async def _open_windows(db, user_id) -> None:
    now = datetime.now()
    for kind, duration in ((WINDOW_5H, timedelta(hours=5)), (WINDOW_WEEKLY, timedelta(days=7))):
        db.add(
            EntitlementWindow(
                user_id=user_id, kind=kind, started_at=now - timedelta(hours=1), expires_at=now + duration
            )
        )
    await db.flush()


async def _window_count(db, user_id) -> int:
    rows = (await db.execute(select(EntitlementWindow).where(EntitlementWindow.user_id == user_id))).scalars().all()
    return len(rows)


async def _credit(db, user_id, amount) -> None:
    db.add(
        CreditTransaction(
            user_id=user_id,
            amount=amount,
            amount_left=amount,
            provider=CreditTransactionProvider.voucher,
            status=CreditTransactionStatus.completed,
            is_active=True,
        )
    )
    await db.flush()


def _completed(order_id: str) -> PaymentEvent:
    return PaymentEvent(
        provider="fake",
        type=PaymentEventType.order_completed,
        provider_event_id=f"ORDER_COMPLETED:{order_id}",
        provider_subscription_id="psub_1",
        order_id=order_id,
    )


@pytest.mark.asyncio
async def test_reset_windows_only_touches_that_user(db):
    user, other = await _user(db), await _user(db)
    await _open_windows(db, user.id)
    await _open_windows(db, other.id)

    await reset_windows(db, user.id)

    assert await _window_count(db, user.id) == 0
    assert await _window_count(db, other.id) == 2


@pytest.mark.asyncio
async def test_credits_subscribe_resets_windows(db):
    user = await _user(db)
    await _credit(db, user.id, 100.0)
    await _open_windows(db, user.id)

    await CreditSubscriptionService.subscribe(db, user, "plus")

    assert await _window_count(db, user.id) == 0


@pytest.mark.asyncio
async def test_credits_upgrade_resets_windows(db):
    user = await _user(db)
    await _credit(db, user.id, 100.0)
    await CreditSubscriptionService.subscribe(db, user, "go")
    await _open_windows(db, user.id)

    await CreditSubscriptionService.upgrade(db, user, "plus")

    assert await _window_count(db, user.id) == 0


@pytest.mark.asyncio
async def test_credits_renewal_keeps_windows(db):
    user = await _user(db)
    await _credit(db, user.id, 100.0)
    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    await _open_windows(db, user.id)
    now = datetime.now()
    sub.current_period_start = now - timedelta(days=31)
    sub.current_period_end = now - timedelta(minutes=1)
    await db.flush()

    assert await CreditSubscriptionService.process_renewals(db, now=now) == 1

    assert sub.status == "active"
    assert await _window_count(db, user.id) == 2


@pytest.mark.asyncio
async def test_provider_activation_resets_windows(db):
    user = await _user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await _open_windows(db, user.id)

    await mgr.start_checkout(Owner.for_user(user), tier="plus", redirect_url="http://x", currency="USD")
    await mgr.handle_event(_completed("setup_1"))

    assert await mgr.current_tier(Owner.for_user(user)) == "plus"
    assert await _window_count(db, user.id) == 0


@pytest.mark.asyncio
async def test_provider_renewal_keeps_windows(db):
    user = await _user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(Owner.for_user(user), tier="plus", redirect_url="http://x", currency="USD")
    await mgr.handle_event(_completed("setup_1"))
    await _open_windows(db, user.id)

    await mgr.handle_event(_completed("renew_1"))

    assert await _window_count(db, user.id) == 2


@pytest.mark.asyncio
async def test_provider_overdue_recovery_resets_windows(db):
    user = await _user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(Owner.for_user(user), tier="plus", redirect_url="http://x", currency="USD")
    await mgr.handle_event(_completed("setup_1"))
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_failed,
            provider_event_id="ORDER_PAYMENT_DECLINED:renew_1",
            provider_subscription_id="psub_1",
            order_id="renew_1",
        )
    )
    await _open_windows(db, user.id)

    await mgr.handle_event(_completed("renew_1"))

    assert await mgr.current_tier(Owner.for_user(user)) == "plus"
    assert await _window_count(db, user.id) == 0


@pytest.mark.asyncio
async def test_provider_upgrade_resets_windows(db):
    user = await _user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    await mgr.start_checkout(Owner.for_user(user), tier="go", redirect_url="http://x", currency="USD")
    await mgr.handle_event(_completed("setup_1"))
    await _open_windows(db, user.id)

    checkout = await mgr.upgrade(Owner.for_user(user), new_tier="plus", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id=f"ORDER_COMPLETED:{checkout.order_id}",
            provider_subscription_id=checkout.provider_subscription_id,
            order_id=checkout.order_id,
        )
    )

    assert await mgr.current_tier(Owner.for_user(user)) == "plus"
    assert await _window_count(db, user.id) == 0


@pytest.mark.asyncio
async def test_trial_start_resets_windows(db):
    user = await _user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await _open_windows(db, user.id)

    await mgr.grant_trial(Owner.for_user(user), tier="plus", days=7, granted_by="admin")

    assert await _window_count(db, user.id) == 0
