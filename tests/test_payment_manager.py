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
from src.services.payments.manager import PaymentManager, _topup_external_ref


class FakeProvider(PaymentProvider):
    """In-memory provider supporting both top-ups and subscriptions."""

    def __init__(self):
        self.order_seq = 0
        self.sub_seq = 0
        self.cancelled: list[str] = []
        self.plan_changes: list[tuple[str, str]] = []
        self.sub_currencies: list[str] = []
        self.topups: list[tuple[float, str]] = []

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
            state="active",
            current_cycle_start=(now - timedelta(days=10)).isoformat(),
            current_cycle_end=(now + timedelta(days=20)).isoformat(),
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


@pytest.mark.asyncio
async def test_upgrade_parks_then_cancels_old(db):
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

    # Upgrade to plus -> parks old as "upgrading", new pending sub created.
    await mgr.upgrade(user, new_tier="plus", redirect_url="http://x", currency="USD")
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
    new_sub = await mgr._active_subscription(user.id, lock=False)
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
