"""Owner-parameterized PaymentManager behavior: liberclaw rows sharing the LTAI state machine."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update

from src.config import config
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.services.payments.base import PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager
from src.services.payments.owner import Owner
from src.subscription_tiers import PRODUCT_LIBERCLAW, PRODUCT_LIBERTAI
from tests.test_payment_manager import FakeProvider


@pytest.mark.asyncio
async def test_lclw_activation_no_email_sent(db, monkeypatch):
    """LTAI-only lifecycle emails must not fire for a liberclaw activation."""
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_lclw_1",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()

    sent = []

    async def fake_send(*args, **kwargs):
        sent.append(args)
        return True

    monkeypatch.setattr("src.services.payments.manager.send_lifecycle_email", fake_send)

    mgr = PaymentManager(FakeProvider(), db)
    # order_id omitted: the liberclaw invoice path is not wired on this branch (see
    # test_lclw_invoice_path_raises_when_reached) and must not be reached here.
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:lclw_1",
            provider_subscription_id="psub_lclw_1",
        )
    )

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "active"
    assert sent == []


@pytest.mark.asyncio
async def test_lclw_invoice_path_raises_when_reached(db):
    """Liberclaw invoicing is not wired on this path; reaching it must fail the webhook
    loudly (provider redelivers) rather than silently activate with no invoice."""
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_lclw_2",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()

    provider = FakeProvider()
    provider.orders["lclw_order_1"] = {
        "amount": 700,
        "currency": "EUR",
        "type": "payment",
        "completed_at": "2026-01-01T00:00:00+00:00",
    }
    mgr = PaymentManager(provider, db)

    with pytest.raises(RuntimeError, match="liberclaw invoicing"):
        await mgr.handle_event(
            PaymentEvent(
                provider="fake",
                type=PaymentEventType.order_completed,
                provider_event_id="ORDER_COMPLETED:lclw_2",
                provider_subscription_id="psub_lclw_2",
                order_id="lclw_order_1",
            )
        )


@pytest.mark.asyncio
async def test_lock_key_uses_account_id_for_lclw(db, monkeypatch):
    account_id = uuid.uuid4()
    owner = Owner.for_liberclaw(account_id, email="a@b.com")
    mgr = PaymentManager(FakeProvider(), db)

    captured: list[Owner] = []
    original = mgr._lock_owner

    async def capture(o: Owner) -> None:
        captured.append(o)
        await original(o)

    monkeypatch.setattr(mgr, "_lock_owner", capture)

    await mgr.start_checkout(owner, tier="starter", redirect_url="http://x", currency="EUR")

    assert len(captured) == 1
    assert captured[0].product == PRODUCT_LIBERCLAW
    assert captured[0].lock_id == account_id


@pytest.mark.asyncio
async def test_supersede_scoped_by_product(db):
    """A liberclaw activation must never cancel an unrelated LTAI row, even one whose raw id
    collides with the liberclaw account id: ``sub_filter()`` disambiguates on ``product``."""
    collision_id = uuid.uuid4()
    ltai_user = User(email=f"{collision_id.hex}@example.com")
    ltai_user.id = collision_id
    db.add(ltai_user)
    await db.flush()

    ltai_sub = PlanSubscription(
        user_id=collision_id,
        tier="plus",
        provider="fake",
        status="active",
        provider_subscription_id="psub_ltai_active",
        current_period_start=datetime.now() - timedelta(days=1),
        current_period_end=datetime.now() + timedelta(days=29),
    )
    db.add(ltai_sub)
    await db.flush()
    db.add(PlanSubscriptionEvent(subscription_id=ltai_sub.id, event_type="activated"))

    lclw_sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_lclw_new",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=collision_id,
    )
    db.add(lclw_sub)
    await db.flush()

    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    lclw_owner = Owner.for_liberclaw(collision_id, email="c@d.com")
    superseded, upgraded_from = await mgr._supersede_other_subs(lclw_owner, exclude_sub_id=lclw_sub.id)

    assert superseded is True
    assert upgraded_from is None
    assert provider.cancelled == []  # the LTAI row was never a candidate

    refreshed_ltai = await db.get(PlanSubscription, ltai_sub.id)
    assert refreshed_ltai.status == "active"
    assert refreshed_ltai.product == PRODUCT_LIBERTAI


@pytest.mark.asyncio
async def test_lclw_checkout_passes_liberclaw_product_to_provider(db):
    """The manager must forward the owner's product to the provider: plan ids for liberclaw
    tiers (e.g. "starter") live only in the liberclaw registry, never the libertai one."""
    account_id = uuid.uuid4()
    owner = Owner.for_liberclaw(account_id, email="a@b.com")
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.start_checkout(owner, tier="starter", redirect_url="http://x", currency="EUR")

    assert provider.sub_products == [PRODUCT_LIBERCLAW]


@pytest.mark.asyncio
async def test_sweep_abandoned_upgrade_checkouts_scoped_by_flag(db, monkeypatch):
    """product_scope() gates the sweep: a liberclaw row is invisible while the cutover flag
    is off, and swept like any abandoned checkout once it flips on."""
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending_upgrade",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()
    await db.execute(
        update(PlanSubscription)
        .where(PlanSubscription.id == sub.id)
        .values(updated_at=datetime.now() - timedelta(hours=25))
    )

    mgr = PaymentManager(FakeProvider(), db)

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    await mgr.sweep_abandoned_upgrade_checkouts()
    await db.refresh(sub)
    assert sub.status == "pending_upgrade"

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    await mgr.sweep_abandoned_upgrade_checkouts()
    await db.refresh(sub)
    assert sub.status == "expired"


@pytest.mark.asyncio
async def test_check_expirations_pass0_scoped_by_flag(db, monkeypatch):
    """product_scope() gates check_expirations' deferred provider-cancel pass. The fake
    provider must report the cycle as already ended, or pass 0 defers the cancel regardless
    of the flag and a still-active row would not be evidence of a gating failure."""
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="active",
        provider_subscription_id="psub_lclw_cancel",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
        cancel_at_period_end=True,
        current_period_end=datetime.now() + timedelta(hours=1),
    )
    db.add(sub)
    await db.flush()

    provider = FakeProvider()
    provider.cycle_end_days = 1 / 24  # provider agrees the cycle ends within the hour
    mgr = PaymentManager(provider, db)

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    await mgr.check_expirations()
    assert provider.cancelled == []

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    await mgr.check_expirations()
    assert provider.cancelled == ["psub_lclw_cancel"]


class _NoOrderIdReconcileProvider(FakeProvider):
    """missed_activation_event without an order_id, so the (unwired) liberclaw invoice path
    is not exercised — this test is about product_scope()'s row visibility, not invoicing."""

    async def missed_activation_event(self, provider_subscription_id: str) -> PaymentEvent | None:
        if self.sub_state not in ("active", "overdue", "paused"):
            return None
        return PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id=f"RECONCILED:{provider_subscription_id}",
            provider_subscription_id=provider_subscription_id,
        )


@pytest.mark.asyncio
async def test_reconcile_pending_scoped_by_flag(db, monkeypatch):
    """product_scope() gates reconcile_pending: a liberclaw row paid at the provider is not
    adopted while the flag is off, and adopted like any missed webhook once it flips on."""
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_lclw_reconcile",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()

    provider = _NoOrderIdReconcileProvider()
    provider.sub_state = "active"  # paid and live at the provider
    mgr = PaymentManager(provider, db)

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    assert await mgr.reconcile_pending() == 0
    await db.refresh(sub)
    assert sub.status == "pending"

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    assert await mgr.reconcile_pending() == 1
    await db.refresh(sub)
    assert sub.status == "active"
