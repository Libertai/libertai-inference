"""PaymentManager state machine: top-ups + subscriptions, with a fake provider."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update

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
from src.services.payments.manager import PaymentManager, SupersedeFailed, _topup_external_ref


class FakeProvider(PaymentProvider):
    """In-memory provider supporting both top-ups and subscriptions."""

    def __init__(self):
        self.order_seq = 0
        self.sub_seq = 0
        self.cancelled: list[str] = []
        self.plan_changes: list[tuple[str, str]] = []
        self.sub_currencies: list[str] = []
        self.topups: list[tuple[float, str]] = []
        self.sub_state = "pending"  # provider-side state reported by get_subscription

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="fake",
            kind=PaymentProviderKind.fiat,
            label="Fake",
            capabilities=[PaymentCapability.topup, PaymentCapability.subscription],
            currencies=["USD"],
        )

    async def create_topup(
        self, *, amount, currency, redirect_url, user_email=None, metadata=None, vat_rate=0.0, item_name="Prepaid credits"
    ):
        self.order_seq += 1
        self.topups.append((amount, currency))
        return CheckoutResult(checkout_url="http://pay/topup", order_id=f"ord_{self.order_seq}")

    async def create_subscription(self, *, user_email, tier, currency, redirect_url, provider_customer_id=None):
        self.sub_seq += 1
        self.sub_currencies.append(currency)
        return CheckoutResult(
            checkout_url="http://pay/sub",
            provider_subscription_id=f"psub_{self.sub_seq}",
            provider_customer_id=provider_customer_id or "cust_1",
            order_id=f"setup_{self.sub_seq}",
        )

    async def cancel_subscription(self, provider_subscription_id: str) -> None:
        self.cancelled.append(provider_subscription_id)

    async def change_subscription_plan(self, provider_subscription_id: str, *, tier: str, currency: str) -> None:
        self.plan_changes.append((provider_subscription_id, tier, currency))

    async def get_subscription(self, provider_subscription_id: str) -> SubscriptionInfo:
        # Dynamic 30-day cycle: 10 days in, 20 days left (keeps remainder math stable over time).
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        return SubscriptionInfo(
            provider_subscription_id=provider_subscription_id,
            state=self.sub_state,
            current_cycle_start=(now - timedelta(days=10)).isoformat(),
            current_cycle_end=(now + timedelta(days=20)).isoformat(),
        )

    async def get_order(self, order_id: str) -> dict:
        return {}


async def _make_user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    await db.flush()
    return user


async def _balance(db, user_id) -> float:
    total = (
        await db.execute(
            select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.is_active == True,
                CreditTransaction.status == CreditTransactionStatus.completed,
            )
        )
    ).scalar()
    return float(total or 0.0)


@pytest.mark.asyncio
async def test_topup_completes_once_and_dedups(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)

    result = await mgr.start_topup(
        user, redirect_url="http://x", usd_credits=10.0, charge_amount=10.0, charge_currency="USD"
    )
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
    await mgr.start_topup(user, redirect_url="http://x", usd_credits=5.0, charge_amount=5.0, charge_currency="USD")

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                     provider_event_id="ORDER_FAILED:ord_1", order_id="ord_1")
    )
    tx = (
        await db.execute(
            select(CreditTransaction).where(
                CreditTransaction.external_reference == _topup_external_ref("fake", "ord_1")
            )
        )
    ).scalar_one()
    assert tx.status == CreditTransactionStatus.error
    assert await _balance(db, user.id) == 0.0


@pytest.mark.asyncio
async def test_topup_declined_then_captured_on_retry_credits_in_full(db):
    """Several declines on one order, then a success: the retry must credit the full amount.

    Revolut retries payments under the same order id, so the pending row is voided by each
    decline before the successful attempt completes it.
    """
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_topup(user, redirect_url="http://x", usd_credits=10.0, charge_amount=10.0, charge_currency="USD")

    for i in range(3):
        await mgr.handle_event(
            PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                         provider_event_id=f"ORDER_PAYMENT_DECLINED:ord_1:{i}", order_id="ord_1")
        )
    assert await _balance(db, user.id) == 0.0

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:ord_1", order_id="ord_1")
    )

    tx = (
        await db.execute(
            select(CreditTransaction).where(
                CreditTransaction.external_reference == _topup_external_ref("fake", "ord_1")
            )
        )
    ).scalar_one()
    assert tx.status == CreditTransactionStatus.completed
    assert tx.is_active is True
    assert tx.amount_left == 10.0
    assert await _balance(db, user.id) == 10.0


@pytest.mark.asyncio
async def test_late_decline_does_not_confiscate_completed_topup(db):
    """Out-of-order delivery: a decline arriving after the order was paid must not void credits."""
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_topup(user, redirect_url="http://x", usd_credits=10.0, charge_amount=10.0, charge_currency="USD")

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:ord_1", order_id="ord_1")
    )
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                     provider_event_id="ORDER_PAYMENT_DECLINED:ord_1", order_id="ord_1")
    )
    assert await _balance(db, user.id) == 10.0


@pytest.mark.asyncio
async def test_topup_eur_pack_charges_eur_but_records_usd_credits(db):
    """EU packs: the provider is charged the gross EUR figure, the pending row records USD credits."""
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.start_topup(user, redirect_url="http://x", usd_credits=10.0, charge_amount=12.0, charge_currency="EUR")
    assert provider.topups == [(12.0, "EUR")]

    tx = (
        await db.execute(
            select(CreditTransaction).where(
                CreditTransaction.external_reference == _topup_external_ref("fake", "ord_1")
            )
        )
    ).scalar_one()
    assert tx.amount == 10.0
    assert tx.amount_left == 10.0
    assert tx.status == CreditTransactionStatus.pending


@pytest.mark.asyncio
async def test_topup_usd_charges_and_records_same_amount(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.start_topup(user, redirect_url="http://x", usd_credits=15.0, charge_amount=15.0, charge_currency="USD")
    assert provider.topups == [(15.0, "USD")]

    tx = (
        await db.execute(
            select(CreditTransaction).where(
                CreditTransaction.external_reference == _topup_external_ref("fake", "ord_1")
            )
        )
    ).scalar_one()
    assert tx.amount == 15.0
    assert tx.amount_left == 15.0


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"usd_credits": 0.0, "charge_amount": 12.0, "charge_currency": "EUR"},
    {"usd_credits": 10.0, "charge_amount": 0.0, "charge_currency": "EUR"},
    {"usd_credits": -1.0, "charge_amount": 12.0, "charge_currency": "EUR"},
])
async def test_topup_rejects_non_positive_amounts(db, kwargs):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    with pytest.raises(ValueError):
        await mgr.start_topup(user, redirect_url="http://x", **kwargs)


@pytest.mark.asyncio
async def test_subscribe_activates_tier(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)

    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")
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
    await mgr.start_checkout(user, tier="go", redirect_url="http://x", currency="USD")

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


async def _event_types(db, sub_id) -> list[str]:
    return list(
        (
            await db.execute(
                select(PlanSubscriptionEvent.event_type)
                .where(PlanSubscriptionEvent.subscription_id == sub_id)
                .order_by(PlanSubscriptionEvent.created_at)
            )
        ).scalars()
    )


@pytest.mark.asyncio
async def test_declined_card_at_checkout_keeps_sub_pending(db):
    """A card declined on the hosted checkout is not a subscription payment failure.

    The sub was never active, so there is nothing to be overdue about — and the user
    typically retries on the same order, which then completes.
    """
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                     provider_event_id="ORDER_PAYMENT_DECLINED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1",
                     metadata={"order_id": "setup_1"})
    )
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.status == "pending"
    assert await _event_types(db, sub.id) == ["created", "checkout_declined"]

    # Retry on the same order succeeds -> normal activation.
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1",
                     metadata={"order_id": "setup_1"})
    )
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.status == "active"
    assert await mgr.current_tier(user.id) == "plus"


@pytest.mark.asyncio
async def test_late_failure_for_completed_order_does_not_revoke_sub(db):
    """Webhooks are not ordered: a declined attempt can land after the order completed."""
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1",
                     metadata={"order_id": "setup_1"})
    )
    # The earlier declined attempt on the SAME order arrives late.
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                     provider_event_id="ORDER_PAYMENT_DECLINED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1",
                     metadata={"order_id": "setup_1"})
    )

    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.status == "active"
    assert await mgr.current_tier(user.id) == "plus"
    assert "payment_failed" not in await _event_types(db, sub.id)


@pytest.mark.asyncio
async def test_renewal_failure_marks_overdue(db):
    """A failed renewal (a NEW order on an active sub) still goes overdue and loses the tier."""
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1",
                     metadata={"order_id": "setup_1"})
    )

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                     provider_event_id="ORDER_PAYMENT_FAILED:renew_1",
                     provider_subscription_id="psub_1", order_id="renew_1",
                     metadata={"order_id": "renew_1"})
    )

    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.status == "overdue"
    assert "payment_failed" in await _event_types(db, sub.id)
    assert await mgr.current_tier(user.id) == "free"


@pytest.mark.asyncio
async def test_declined_card_on_upgrade_checkout_stays_pending_upgrade(db):
    """``overdue`` sits inside the live-subscription index: writing it on an upgrade

    checkout would collide with the still-active subscription it is meant to replace.
    """
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    db.add(PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="active",
        provider_subscription_id="psub_old",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    ))
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add(checkout)
    await db.flush()

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                     provider_event_id="ORDER_PAYMENT_DECLINED:ord_9",
                     provider_subscription_id="psub_new", order_id="ord_9",
                     metadata={"order_id": "ord_9"})
    )

    await db.refresh(checkout)
    assert checkout.status == "pending_upgrade"
    assert "checkout_declined" in await _event_types(db, checkout.id)


@pytest.mark.asyncio
async def test_subscription_overdue_ignored_on_unpaid_checkout(db):
    """A provider-side SUBSCRIPTION_OVERDUE on an unpaid checkout row is not a card decline —

    the row is left alone rather than pushed into ``overdue``, which is inside the
    live-subscription index and would collide with the active row it is replacing.
    """
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add(checkout)
    await db.flush()

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.subscription_overdue,
                     provider_event_id="SUBSCRIPTION_OVERDUE:",
                     provider_subscription_id="psub_new", order_id=None,
                     metadata={"raw_event": "SUBSCRIPTION_OVERDUE"})
    )

    await db.refresh(checkout)
    assert checkout.status == "pending_upgrade"
    assert "overdue_ignored_unpaid_checkout" in await _event_types(db, checkout.id)


@pytest.mark.asyncio
async def test_upgrade_cancels_old_sub_once_the_new_one_is_paid(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    # Active go sub.
    await mgr.start_checkout(user, tier="go", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1")
    )
    assert await mgr.current_tier(user.id) == "go"

    # Upgrade to plus -> old sub stays active, new pending_upgrade checkout created.
    await mgr.upgrade(user, new_tier="plus", redirect_url="http://x", currency="USD")
    live = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.user_id == user.id, PlanSubscription.status == "active"
            )
        )
    ).scalar_one()
    assert live.tier == "go"

    # Pay the new sub -> activates plus, old gets cancelled.
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_2",
                     provider_subscription_id="psub_2", order_id="setup_2")
    )
    assert await mgr.current_tier(user.id) == "plus"
    assert "psub_1" in provider.cancelled
    refreshed_old = await db.get(PlanSubscription, live.id)
    assert refreshed_old.status == "cancelled"


@pytest.mark.asyncio
async def test_completed_upgrade_supersedes_and_prorates(db):
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    old = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="active",
        provider_subscription_id="psub_old",
        current_period_start=datetime.now() - timedelta(days=10),
        current_period_end=datetime.now() + timedelta(days=20),
    )
    new = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add_all([old, new])
    await db.flush()

    await manager.handle_event(PaymentEvent(
        provider="fake", type=PaymentEventType.order_completed,
        provider_event_id="ORDER_COMPLETED:ord_1",
        provider_subscription_id="psub_new", order_id="ord_1", metadata={},
    ))

    await db.refresh(old)
    await db.refresh(new)
    assert new.status == "active"
    assert old.status == "cancelled"
    assert "psub_old" in provider.cancelled
    assert await _balance(db, user.id) > 0  # unused remainder credited


@pytest.mark.asyncio
async def test_activation_raises_when_the_paid_sub_cannot_be_cancelled(db):
    """A paid row that will not cancel at the provider stays live, so activating on top of it
    would put two rows in the one-live-subscription index. The activation raises instead,
    aborting the webhook transaction so the provider retries against unchanged state."""

    class UncancellableProvider(FakeProvider):
        async def cancel_subscription(self, provider_subscription_id: str) -> None:
            raise RuntimeError("provider down")

    user = await _make_user(db)
    provider = UncancellableProvider()
    manager = PaymentManager(provider, db)
    old = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="active",
        provider_subscription_id="psub_old",
        current_period_start=datetime.now() - timedelta(days=10),
        current_period_end=datetime.now() + timedelta(days=20),
    )
    new = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add_all([old, new])
    await db.flush()

    with pytest.raises(SupersedeFailed):
        await manager.handle_event(PaymentEvent(
            provider="fake", type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:ord_1",
            provider_subscription_id="psub_new", order_id="ord_1", metadata={},
        ))

    # Nothing was written before the raise, so the state the retry will see is untouched.
    await db.refresh(old)
    await db.refresh(new)
    assert old.status == "active"  # entitlement stays where it was paid for
    assert new.status == "pending_upgrade"
    assert await _balance(db, user.id) == 0.0  # no remainder credited for a cycle still running
    assert "activated" not in await _event_types(db, new.id)


async def _dateless_paid_pair(db) -> tuple[User, PlanSubscription, PlanSubscription]:
    """A user whose live paid row lost its period dates (a swallowed provider read at
    activation), plus the upgrade checkout about to be paid."""
    user = await _make_user(db)
    old = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="active",
        provider_subscription_id="psub_old",
    )
    new = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add_all([old, new])
    await db.flush()
    db.add(PlanSubscriptionEvent(subscription_id=old.id, event_type="activated"))
    await db.flush()
    return user, old, new


@pytest.mark.asyncio
async def test_supersede_treats_a_dateless_row_with_an_activation_as_paid(db):
    """Period dates are not proof of payment: an activated row that never got them must be
    cancelled and prorated like any paid sub, not expired as an abandoned checkout."""
    user, old, new = await _dateless_paid_pair(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)

    await manager.handle_event(PaymentEvent(
        provider="fake", type=PaymentEventType.order_completed,
        provider_event_id="ORDER_COMPLETED:ord_1",
        provider_subscription_id="psub_new", order_id="ord_1", metadata={},
    ))

    await db.refresh(old)
    await db.refresh(new)
    assert new.status == "active"
    assert old.status == "cancelled"
    old_events = await _event_types(db, old.id)
    assert "cancelled_for_upgrade" in old_events
    assert "expired_abandoned_checkout" not in old_events
    assert "upgraded" in await _event_types(db, new.id)


@pytest.mark.asyncio
async def test_supersede_aborts_when_a_dateless_paid_row_cannot_be_cancelled(db):
    """Same row, failing cancel: activating anyway would leave two rows live."""

    class UncancellableProvider(FakeProvider):
        async def cancel_subscription(self, provider_subscription_id: str) -> None:
            raise RuntimeError("provider down")

    user, old, new = await _dateless_paid_pair(db)
    manager = PaymentManager(UncancellableProvider(), db)

    with pytest.raises(SupersedeFailed):
        await manager.handle_event(PaymentEvent(
            provider="fake", type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:ord_1",
            provider_subscription_id="psub_new", order_id="ord_1", metadata={},
        ))

    await db.refresh(old)
    await db.refresh(new)
    assert old.status == "active"
    assert new.status == "pending_upgrade"
    assert "expired_abandoned_checkout" not in await _event_types(db, old.id)


@pytest.mark.asyncio
async def test_redelivery_caught_after_the_lock_when_the_early_read_misses(db, monkeypatch):
    """Two redeliveries of one event can both clear the unlocked dedup read and then serialize
    on the mutex. The suite's single pooled connection cannot express that concurrency, so the
    second delivery's view is simulated by blinding its first read; the post-lock check is what
    has to stop it before it re-runs as a renewal."""
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")
    event = PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                         provider_event_id="ORDER_COMPLETED:setup_1",
                         provider_subscription_id="psub_1", order_id="setup_1",
                         metadata={"order_id": "setup_1"})
    await mgr.handle_event(event)

    sub = await mgr._active_subscription(user.id, lock=False)
    await db.refresh(sub)
    events_before = await _event_types(db, sub.id)
    period_end_before = sub.current_period_end

    real_check = mgr._is_duplicate_event
    calls = []

    async def blind_first(evt):
        calls.append(evt.provider_event_id)
        return False if len(calls) == 1 else await real_check(evt)

    monkeypatch.setattr(mgr, "_is_duplicate_event", blind_first)
    await mgr.handle_event(event)  # must not raise

    assert len(calls) == 2  # the post-lock read is what caught it
    await db.refresh(sub)
    assert await _event_types(db, sub.id) == events_before  # no second "renewed" logged
    assert sub.current_period_end == period_end_before  # billing cycle not advanced twice


@pytest.mark.asyncio
async def test_renewal_does_not_retire_an_open_upgrade_checkout(db):
    """ORDER_COMPLETED also fires for renewals; the checkout must survive one."""
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    old = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="active",
        provider_subscription_id="psub_old",
        current_period_start=datetime.now() - timedelta(days=30),
        current_period_end=datetime.now(),
    )
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add_all([old, checkout])
    await db.flush()
    db.add(PlanSubscriptionEvent(subscription_id=old.id, event_type="activated"))
    await db.flush()

    await manager.handle_event(PaymentEvent(
        provider="fake", type=PaymentEventType.order_completed,
        provider_event_id="ORDER_COMPLETED:ord_renew",
        provider_subscription_id="psub_old", order_id="ord_renew", metadata={},
    ))

    await db.refresh(checkout)
    assert checkout.status == "pending_upgrade"
    assert "psub_new" not in provider.cancelled


@pytest.mark.asyncio
async def test_refuses_to_activate_a_retired_checkout(db):
    """A payment landing on a row we already expired must not supersede the live sub."""
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    live = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="active",
        provider_subscription_id="psub_live",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    )
    retired = PlanSubscription(
        user_id=user.id, tier="go", provider="fake", status="expired",
        provider_subscription_id="psub_retired",
    )
    db.add_all([live, retired])
    await db.flush()
    db.add(PlanSubscriptionEvent(subscription_id=retired.id, event_type="expired_abandoned_checkout"))
    await db.flush()

    await manager.handle_event(PaymentEvent(
        provider="fake", type=PaymentEventType.order_completed,
        provider_event_id="ORDER_COMPLETED:ord_stale",
        provider_subscription_id="psub_retired", order_id="ord_stale", metadata={},
    ))

    await db.refresh(live)
    await db.refresh(retired)
    assert live.status == "active"
    assert retired.status == "expired"
    assert provider.cancelled == []
    events = (await db.execute(
        select(PlanSubscriptionEvent.event_type).where(PlanSubscriptionEvent.subscription_id == retired.id)
    )).scalars().all()
    assert "activated" not in events  # a refused row must never carry an activation


@pytest.mark.asyncio
async def test_refused_activation_is_recorded_as_an_audit_event(db):
    """Container logs only retain since the last deploy — the refusal must survive as a
    queryable DB row so an operator can find and credit the charge."""
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    retired = PlanSubscription(
        user_id=user.id, tier="go", provider="fake", status="expired",
        provider_subscription_id="psub_retired",
    )
    db.add(retired)
    await db.flush()
    db.add(PlanSubscriptionEvent(subscription_id=retired.id, event_type="expired_abandoned_checkout"))
    await db.flush()

    await manager.handle_event(PaymentEvent(
        provider="fake", type=PaymentEventType.order_completed,
        provider_event_id="ORDER_COMPLETED:ord_stale",
        provider_subscription_id="psub_retired", order_id="ord_stale", metadata={},
    ))

    refusal = (await db.execute(
        select(PlanSubscriptionEvent)
        .where(
            PlanSubscriptionEvent.subscription_id == retired.id,
            PlanSubscriptionEvent.event_type == "activation_refused",
        )
    )).scalar_one()
    assert refusal.metadata_json == {"order_id": "ord_stale"}


@pytest.mark.asyncio
async def test_refusal_survives_a_retirement_committed_during_the_lock_wait(db, monkeypatch):
    """The first refusal check reads outside the per-user mutex, so it can miss a retirement
    that commits while the webhook queues for it. Simulate that by blinding the first read."""
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    live = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="active",
        provider_subscription_id="psub_live",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    )
    retired = PlanSubscription(
        user_id=user.id, tier="go", provider="fake", status="expired",
        provider_subscription_id="psub_retired",
    )
    db.add_all([live, retired])
    await db.flush()
    db.add(PlanSubscriptionEvent(subscription_id=retired.id, event_type="expired_abandoned_checkout"))
    await db.flush()

    real_check = manager._is_retired_checkout
    calls = []

    async def blind_first(sub):
        calls.append(sub.id)
        return False if len(calls) == 1 else await real_check(sub)

    monkeypatch.setattr(manager, "_is_retired_checkout", blind_first)

    await manager.handle_event(PaymentEvent(
        provider="fake", type=PaymentEventType.order_completed,
        provider_event_id="ORDER_COMPLETED:ord_stale",
        provider_subscription_id="psub_retired", order_id="ord_stale", metadata={},
    ))

    assert len(calls) == 2  # the post-lock read is what refused it
    await db.refresh(live)
    await db.refresh(retired)
    assert live.status == "active"
    assert retired.status == "expired"
    assert provider.cancelled == []
    assert "activated" not in await _event_types(db, retired.id)


@pytest.mark.asyncio
async def test_cancel_sets_period_end_flag(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1")
    )

    res = await mgr.cancel(user)
    assert "end of billing period" in res["message"]
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.cancel_at_period_end is True
    assert sub.pending_tier == "free"  # cancel == downgrade to free (drives the plans UI)
    assert sub.status == "active"  # still active until period end
    # Provider-side cancel is DEFERRED (terminal on Revolut) so the user can resume.
    assert provider.cancelled == []


@pytest.mark.parametrize("action,arg", [("cancel", None), ("downgrade", "free"), ("downgrade", "go")])
@pytest.mark.asyncio
async def test_wind_down_retires_the_open_upgrade_checkout(db, action, arg):
    """Otherwise the user winds down, pays the still-live link, and silently ends up on a
    renewing, more expensive plan."""
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    db.add(PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="active",
        provider_subscription_id="psub_old", currency="USD",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    ))
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add(checkout)
    await db.flush()

    if action == "cancel":
        await manager.cancel(user)
    else:
        await manager.request_downgrade(user, arg)

    await db.refresh(checkout)
    assert checkout.status == "expired"
    assert "psub_new" in provider.cancelled
    if action == "downgrade" and arg == "go":
        # Paid -> paid downgrade schedules the plan change at the provider on the OLD
        # subscription, not the checkout being retired.
        assert provider.plan_changes == [("psub_old", "go", "USD")]


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["EUR", "USD"])
async def test_start_checkout_threads_currency_to_provider_and_locks_row(db, currency):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.start_checkout(user, tier="go", redirect_url="http://x", currency=currency)
    assert provider.sub_currencies == [currency]
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.currency == currency


@pytest.mark.asyncio
async def test_upgrade_threads_currency(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.start_checkout(user, tier="go", redirect_url="http://x", currency="EUR")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:setup_1",
            provider_subscription_id="psub_1",
            order_id="setup_1",
        )
    )

    await mgr.upgrade(user, new_tier="plus", redirect_url="http://x", currency="EUR")
    assert provider.sub_currencies == ["EUR", "EUR"]
    new_sub = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.user_id == user.id, PlanSubscription.status == "pending_upgrade"
            )
        )
    ).scalar_one()
    assert new_sub.tier == "plus"
    assert new_sub.currency == "EUR"


async def _make_upgrading_sub(db, user, aged_hours: float) -> PlanSubscription:
    """A sub parked as "upgrading", with updated_at pushed ``aged_hours`` into the past."""
    sub = PlanSubscription(
        user_id=user.id,
        tier="go",
        status="upgrading",
        provider="fake",
        provider_subscription_id=f"psub_parked_{user.id}",
        currency="USD",
    )
    db.add(sub)
    await db.flush()
    await db.execute(
        update(PlanSubscription)
        .where(PlanSubscription.id == sub.id)
        .values(updated_at=datetime.now() - timedelta(hours=aged_hours))
    )
    return sub


@pytest.mark.asyncio
async def test_check_expirations_reverts_stale_upgrading_sub(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    sub = await _make_upgrading_sub(db, user, aged_hours=2)

    await mgr.check_expirations()

    assert sub.status == "active"  # entitlement restored
    assert provider.cancelled == []  # nothing touched on the provider
    reverted = (
        await db.execute(
            select(func.count())
            .select_from(PlanSubscriptionEvent)
            .where(
                PlanSubscriptionEvent.subscription_id == sub.id,
                PlanSubscriptionEvent.event_type == "upgrade_abandoned_reverted",
            )
        )
    ).scalar()
    assert reverted == 1


@pytest.mark.asyncio
async def test_check_expirations_keeps_recent_upgrading_sub(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    sub = await _make_upgrading_sub(db, user, aged_hours=0.5)

    await mgr.check_expirations()

    assert sub.status == "upgrading"  # under the 1h threshold — not yet abandoned


@pytest.mark.asyncio
async def test_check_expirations_skips_revert_while_new_checkout_pending(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    sub = await _make_upgrading_sub(db, user, aged_hours=2)
    # The new checkout's row still exists: reverting to "active" would violate the
    # one-active-sub index, so the parked sub must stay "upgrading".
    db.add(
        PlanSubscription(
            user_id=user.id,
            tier="plus",
            status="pending",
            provider="fake",
            provider_subscription_id=f"psub_new_{user.id}",
            currency="USD",
        )
    )
    await db.flush()

    await mgr.check_expirations()

    assert sub.status == "upgrading"


def _paid_event(seq: int) -> PaymentEvent:
    return PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                        provider_event_id=f"ORDER_COMPLETED:cycle_{seq}",
                        provider_subscription_id="psub_1", order_id=f"cycle_{seq}")


async def _active_plus_sub(db, provider) -> tuple:
    """User with an active 'plus' subscription on the fake provider."""
    user = await _make_user(db)
    mgr = PaymentManager(provider, db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1")
    )
    return user, mgr


@pytest.mark.asyncio
async def test_paid_downgrade_schedules_plan_change_not_cancel(db):
    """Plus -> Go on a fiat provider: the provider gets a scheduled plan change to the GO
    variation; the sub keeps renewing (no cancel flag, nothing cancelled on the provider)."""
    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)

    res = await mgr.request_downgrade(user, new_tier="go")
    assert res["new_tier"] == "go"

    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.pending_tier == "go"
    assert sub.cancel_at_period_end is False
    assert provider.cancelled == []
    # The provider was told to switch psub_1 to go in the sub's locked currency.
    assert provider.plan_changes == [("psub_1", "go", "USD")]


@pytest.mark.asyncio
async def test_paid_downgrade_supersedes_earlier_cancel(db):
    """Cancelling then downgrading paid->paid means 'keep me subscribed, on the lower tier'."""
    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)

    await mgr.cancel(user)
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.cancel_at_period_end is True

    await mgr.request_downgrade(user, new_tier="go")
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.cancel_at_period_end is False
    assert sub.pending_tier == "go"


@pytest.mark.asyncio
async def test_downgrade_to_free_still_cancels(db):
    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)

    await mgr.request_downgrade(user, new_tier="free")
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.cancel_at_period_end is True
    assert sub.pending_tier == "free"
    assert provider.cancelled == []  # deferred to the pre-renewal cron pass
    assert provider.plan_changes == []


@pytest.mark.asyncio
async def test_next_cycle_payment_applies_pending_downgrade(db):
    """The first billing of the new cycle (on the lower plan) flips the local tier."""
    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)
    await mgr.request_downgrade(user, new_tier="go")

    await mgr.handle_event(_paid_event(2))

    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.tier == "go"
    assert sub.pending_tier is None
    assert sub.status == "active"
    assert await mgr.current_tier(user.id) == "go"


@pytest.mark.asyncio
async def test_paid_downgrade_provider_failure_leaves_sub_untouched(db):
    """If the provider rejects the plan change, no pending downgrade is recorded."""

    class FailingProvider(FakeProvider):
        async def change_subscription_plan(self, provider_subscription_id: str, *, tier: str, currency: str) -> None:
            raise RuntimeError("provider down")

    provider = FailingProvider()
    user, mgr = await _active_plus_sub(db, provider)

    with pytest.raises(RuntimeError):
        await mgr.request_downgrade(user, new_tier="go")
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.pending_tier is None
    assert sub.cancel_at_period_end is False


@pytest.mark.asyncio
async def test_upgrade_credits_unused_remainder_of_old_cycle(db):
    """Upgrading mid-cycle refunds the unused fraction of the old plan as prepaid credits
    (FakeProvider cycle: 10 of 30 days used -> ~2/3 of go's $8 comes back)."""
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    user = await _make_user(db)

    await mgr.start_checkout(user, tier="go", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1")
    )
    await mgr.upgrade(user, new_tier="plus", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_2",
                     provider_subscription_id="psub_2", order_id="setup_2")
    )

    balance = await _balance(db, user.id)
    assert balance == pytest.approx(8.0 * (20 / 30), abs=0.05)

    # Replays / direct re-runs must not double-credit (per-subscription tx hash).
    old_sub = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.user_id == user.id, PlanSubscription.status == "cancelled"
            )
        )
    ).scalar_one()
    await mgr._credit_unused_remainder(old_sub)
    assert await _balance(db, user.id) == pytest.approx(balance)


@pytest.mark.asyncio
async def test_upgrade_remainder_skipped_without_cycle_dates(db):
    """A parked sub that never activated (no period dates) gets no refund."""
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    user = await _make_user(db)

    sub = PlanSubscription(user_id=user.id, tier="go", status="upgrading", provider="fake",
                           provider_subscription_id="psub_x", currency="USD")
    db.add(sub)
    await db.flush()

    await mgr._credit_unused_remainder(sub)
    assert await _balance(db, user.id) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_resume_clears_scheduled_cancellation(db):
    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)
    await mgr.cancel(user)

    res = await mgr.resume(user)
    assert res["tier"] == "plus"
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.cancel_at_period_end is False
    assert sub.pending_tier is None
    assert provider.cancelled == []  # never touched the provider


@pytest.mark.asyncio
async def test_resume_undoes_paid_downgrade_via_plan_change_back(db):
    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)
    await mgr.request_downgrade(user, new_tier="go")

    await mgr.resume(user)
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.pending_tier is None
    # Second plan change schedules a switch BACK to the current (plus) plan.
    assert provider.plan_changes == [("psub_1", "go", "USD"), ("psub_1", "plus", "USD")]


@pytest.mark.asyncio
async def test_resume_with_nothing_scheduled_rejected(db):
    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)
    with pytest.raises(ValueError, match="Nothing to resume"):
        await mgr.resume(user)


@pytest.mark.asyncio
async def test_deferred_provider_cancel_runs_before_renewal(db):
    """check_expirations cancels on the provider once the period end is near (<=2h),
    while the local sub stays active until the expiry pass."""
    from datetime import datetime, timedelta

    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)
    await mgr.cancel(user)
    assert provider.cancelled == []

    # Pull the period end into the pre-cancel window (naive, matching the columns).
    sub = await mgr._active_subscription(user.id, lock=False)
    sub.current_period_end = datetime.now() + timedelta(hours=1)
    await db.flush()

    await mgr.check_expirations()
    assert "psub_1" in provider.cancelled
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.status == "active"  # entitlement holds until the expiry pass (24h grace)


@pytest.mark.asyncio
async def test_check_expirations_revert_survives_index_collision(db, monkeypatch):
    """The skip-guard is racy by nature (a webhook can activate a sub between the check
    and the write). Simulate the race by blinding the guard: the unique-index violation
    must be contained in a savepoint, leaving the row parked and the session usable."""
    provider = FakeProvider()
    user, mgr = await _active_plus_sub(db, provider)  # active sub occupies the unique index
    parked = await _make_upgrading_sub(db, user, aged_hours=2)
    parked_id = parked.id  # the savepoint rollback expires the instance — read it now

    async def race_blind(*args, **kwargs):
        return None  # the guard "sees" no active sub — exactly the race window

    monkeypatch.setattr(mgr, "_active_subscription", race_blind)
    await mgr.check_expirations()  # must not raise

    refreshed = await db.get(PlanSubscription, parked_id)
    assert refreshed.status == "upgrading"  # collision skipped, row left for the next pass
    # The outer transaction survived the savepoint rollback: writes still work.
    refreshed.tier = "go"
    await db.flush()


@pytest.mark.asyncio
async def test_renewal_cycle_logs_renewed_not_activated(db):
    user = await _make_user(db)
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")

    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:setup_1",
                     provider_subscription_id="psub_1", order_id="setup_1",
                     metadata={"order_id": "setup_1"})
    )
    # Next billing cycle completes on a new order -> renewed, not a second activated.
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_completed,
                     provider_event_id="ORDER_COMPLETED:ren_1",
                     provider_subscription_id="psub_1", order_id="ren_1",
                     metadata={"order_id": "ren_1"})
    )
    sub = await mgr._active_subscription(user.id, lock=False)
    assert await _event_types(db, sub.id) == ["created", "activated", "renewed"]

    # Out-of-order decline for the already-paid renewal order is ignored (sub stays active).
    await mgr.handle_event(
        PaymentEvent(provider="fake", type=PaymentEventType.order_failed,
                     provider_event_id="ORDER_PAYMENT_DECLINED:ren_1",
                     provider_subscription_id="psub_1", order_id="ren_1",
                     metadata={"order_id": "ren_1"})
    )
    sub = await mgr._active_subscription(user.id, lock=False)
    assert sub.status == "active"
    assert await _event_types(db, sub.id) == ["created", "activated", "renewed"]


@pytest.mark.asyncio
async def test_upgrade_leaves_paid_sub_active(db):
    """The fix: an in-flight upgrade must not disturb the subscription being replaced."""
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    old = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="active",
        provider_subscription_id="psub_old",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    )
    db.add(old)
    await db.flush()

    await manager.upgrade(user, "max", "http://redirect", "USD")

    await db.refresh(old)
    assert old.status == "active"
    assert provider.cancelled == []
    new = (await db.execute(
        select(PlanSubscription).where(PlanSubscription.status == "pending_upgrade")
    )).scalar_one()
    assert new.tier == "max"


@pytest.mark.asyncio
async def test_upgrade_requires_a_live_paid_subscription(db):
    user = await _make_user(db)
    manager = PaymentManager(FakeProvider(), db)
    with pytest.raises(ValueError, match="No active subscription"):
        await manager.upgrade(user, "max", "http://redirect", "USD")


@pytest.mark.asyncio
async def test_upgrade_validates_against_the_live_row_tier(db):
    """current_tier() reports free for an overdue row; validating against it would let a
    Max holder open a Go 'upgrade'."""
    user = await _make_user(db)
    manager = PaymentManager(FakeProvider(), db)
    db.add(PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="overdue",
        provider_subscription_id="psub_old",
    ))
    await db.flush()
    with pytest.raises(ValueError, match="Cannot upgrade"):
        await manager.upgrade(user, "go", "http://redirect", "USD")


@pytest.mark.asyncio
async def test_second_upgrade_retires_the_first_checkout(db):
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    db.add(PlanSubscription(
        user_id=user.id, tier="go", provider="fake", status="active",
        provider_subscription_id="psub_old",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    ))
    await db.flush()

    await manager.upgrade(user, "plus", "http://redirect", "USD")
    first = (await db.execute(
        select(PlanSubscription).where(PlanSubscription.status == "pending_upgrade")
    )).scalar_one()
    first_id = first.provider_subscription_id

    await manager.upgrade(user, "max", "http://redirect", "USD")

    await db.refresh(first)
    assert first.status == "expired"
    assert first_id in provider.cancelled
    events = (await db.execute(
        select(PlanSubscriptionEvent.event_type).where(PlanSubscriptionEvent.subscription_id == first.id)
    )).scalars().all()
    assert "expired_abandoned_checkout" in events


@pytest.mark.asyncio
async def test_subscribe_retires_an_orphaned_upgrade_checkout(db):
    """A user whose old sub lapsed mid-upgrade must not end up with two live checkouts."""
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    stale = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_stale",
    )
    db.add(stale)
    await db.flush()

    await manager.start_checkout(user, "plus", "http://redirect", "USD")

    await db.refresh(stale)
    assert stale.status == "expired"
    assert "psub_stale" in provider.cancelled


@pytest.mark.asyncio
async def test_sweep_expires_and_cancels_stale_upgrade_checkout(db):
    user = await _make_user(db)
    provider = FakeProvider()
    manager = PaymentManager(provider, db)
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add(checkout)
    await db.flush()
    await db.execute(
        update(PlanSubscription)
        .where(PlanSubscription.id == checkout.id)
        .values(updated_at=datetime.now() - timedelta(hours=25))
    )

    await manager.sweep_abandoned_upgrade_checkouts()

    await db.refresh(checkout)
    assert checkout.status == "expired"
    assert "psub_new" in provider.cancelled
    events = (await db.execute(
        select(PlanSubscriptionEvent.event_type).where(PlanSubscriptionEvent.subscription_id == checkout.id)
    )).scalars().all()
    assert "expired_abandoned_checkout" in events


@pytest.mark.asyncio
async def test_sweep_keeps_recent_checkout(db):
    user = await _make_user(db)
    manager = PaymentManager(FakeProvider(), db)
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add(checkout)
    await db.flush()

    await manager.sweep_abandoned_upgrade_checkouts()

    await db.refresh(checkout)
    assert checkout.status == "pending_upgrade"


@pytest.mark.asyncio
async def test_sweep_leaves_row_alone_when_provider_cancel_fails(db):
    """Writing `expired` after a failed cancel marks the row dead locally while the link
    stays payable for up to 30 days."""
    user = await _make_user(db)
    provider = FakeProvider()

    async def boom(provider_subscription_id: str) -> None:
        raise RuntimeError("provider down")

    provider.cancel_subscription = boom  # type: ignore[assignment]
    manager = PaymentManager(provider, db)
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new",
    )
    db.add(checkout)
    await db.flush()
    await db.execute(
        update(PlanSubscription)
        .where(PlanSubscription.id == checkout.id)
        .values(updated_at=datetime.now() - timedelta(hours=25))
    )

    await manager.sweep_abandoned_upgrade_checkouts()

    await db.refresh(checkout)
    assert checkout.status == "pending_upgrade"
