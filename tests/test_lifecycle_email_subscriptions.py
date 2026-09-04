"""Subscription-triggered lifecycle emails: welcomes, payment failures, cancellation flow."""

from sqlalchemy import select

from src.models.lifecycle_email_send import LifecycleEmailSend
from src.services.payments.base import PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager
from src.services.payments.owner import Owner
from tests.test_payment_manager import FakeProvider, _make_user


async def _send_types(db, user_id) -> list[str]:
    return list(
        (
            await db.execute(
                select(LifecycleEmailSend.email_type)
                .where(LifecycleEmailSend.user_id == user_id)
                .order_by(LifecycleEmailSend.sent_at)
            )
        ).scalars()
    )


async def _activate(mgr, user, tier: str):
    await mgr.start_checkout(Owner.for_user(user), tier=tier, redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:setup_1",
            provider_subscription_id="psub_1",
            order_id="setup_1",
        )
    )


async def test_activation_sends_welcome_once_across_renewals(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await _activate(mgr, user, "go")
    assert await _send_types(db, user.id) == ["paid_welcome_go"]

    # Renewal cycle completes the same subscription again: no second welcome.
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:ord_renew",
            provider_subscription_id="psub_1",
            order_id="ord_renew",
        )
    )
    assert await _send_types(db, user.id) == ["paid_welcome_go"]


async def test_welcome_type_is_per_tier(db):
    """An upgrade to another tier later still welcomes: dedup is per email_type, not per user."""
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await _activate(mgr, user, "max")
    assert await _send_types(db, user.id) == ["paid_welcome_max"]


async def test_renewal_failure_emails_once_per_incident(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await _activate(mgr, user, "plus")

    for i in range(2):  # provider retries the charge; only the first failure emails
        await mgr.handle_event(
            PaymentEvent(
                provider="fake",
                type=PaymentEventType.order_failed,
                provider_event_id=f"ORDER_FAILED:retry_{i}",
                order_id=f"retry_{i}",
                provider_subscription_id="psub_1",
            )
        )
    types = await _send_types(db, user.id)
    assert types.count("payment_failed") == 1


async def test_declined_checkout_card_sends_no_email(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(Owner.for_user(user), tier="go", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_failed,
            provider_event_id="ORDER_FAILED:setup_1",
            order_id="setup_1",
            provider_subscription_id="psub_1",
        )
    )
    assert await _send_types(db, user.id) == []


async def test_overdue_on_unpaid_checkout_sends_no_email(db):
    """A provider overdue notice on a never-paid checkout row is not a failed charge."""
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(Owner.for_user(user), tier="go", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.subscription_overdue,
            provider_event_id="SUB_OVERDUE:psub_1",
            provider_subscription_id="psub_1",
        )
    )
    assert await _send_types(db, user.id) == []


async def test_overdue_on_live_subscription_emails(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await _activate(mgr, user, "go")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.subscription_overdue,
            provider_event_id="SUB_OVERDUE:psub_1",
            provider_subscription_id="psub_1",
        )
    )
    assert "payment_failed" in await _send_types(db, user.id)


async def test_cancel_sends_confirmation(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await _activate(mgr, user, "go")
    await mgr.cancel(Owner.for_user(user))
    assert "cancellation_confirmed" in await _send_types(db, user.id)
