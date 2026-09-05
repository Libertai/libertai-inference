"""LCLW webhook ownership: invoicing, lc_users.tier sync, and the cancel-echo decision table."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from src.config import config
from src.models.invoice import Invoice
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.services.payments.base import PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager
from src.subscription_tiers import PRODUCT_LIBERCLAW
from tests.test_payment_manager import FakeProvider, _event_types


@pytest.fixture(autouse=True)
def _lclw_billing_cut_over(monkeypatch):
    """Every test here exercises handle_event's LCLW dispatch, which is 200-skipped while
    LIBERCLAW_BILLING_ENABLED is off — cutover-flag gating itself is covered elsewhere."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)


async def _lc_user(db, account_id: uuid.UUID, tier: str = "starter") -> LiberclawUser:
    lc_user = LiberclawUser(user_id=f"{account_id.hex}@example.com", user_type="email", tier=tier)
    lc_user.liberclaw_account_id = account_id
    db.add(lc_user)
    await db.flush()
    return lc_user


def _lclw_sub(account_id: uuid.UUID, **kwargs) -> PlanSubscription:
    defaults = {
        "user_id": None,
        "tier": "starter",
        "provider": "fake",
        "status": "pending",
        "provider_subscription_id": "psub_lclw",
        "product": PRODUCT_LIBERCLAW,
        "liberclaw_account_id": account_id,
    }
    defaults.update(kwargs)
    return PlanSubscription(**defaults)


@pytest.mark.asyncio
async def test_lclw_activation_issues_invoice_and_syncs_tier(db):
    """order_completed for LCLW: issues an LCLW-series invoice carrying cycle_id + the
    LiberClaw-branded label, and pushes the activated tier onto lc_users."""
    account_id = uuid.uuid4()
    await _lc_user(db, account_id, tier="free")
    sub = _lclw_sub(account_id, status="pending")
    db.add(sub)
    await db.flush()

    provider = FakeProvider()
    provider.orders["lclw_order_1"] = {
        "amount": 700,
        "currency": "EUR",
        "type": "payment",
        "completed_at": "2026-01-01T00:00:00+00:00",
        "channel_data": {"subscription_id": "psub_lclw", "subscription_cycle_id": "cycle_1"},
    }
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:lclw_1",
            provider_subscription_id="psub_lclw",
            order_id="lclw_order_1",
        )
    )

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "active"

    invoice = (await db.execute(select(Invoice).where(Invoice.liberclaw_account_id == account_id))).scalar_one()
    assert invoice.series == "LCLW"
    assert invoice.cycle_id == "cycle_1"
    assert invoice.provider_subscription_id == "psub_lclw"
    assert invoice.line_label == "LiberClaw Starter subscription"
    assert invoice.buyer["email"] == f"{account_id.hex}@example.com"

    lc_user = (
        await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
    ).scalar_one()
    assert lc_user.tier == "starter"


@pytest.mark.asyncio
async def test_lclw_activation_without_bridge_row_raises(db):
    """No lc_users row bridges this account id: the invoice email cannot be resolved, so the
    webhook must fail loudly (provider redelivers) rather than silently skip invoicing."""
    account_id = uuid.uuid4()
    sub = _lclw_sub(account_id, status="pending")
    db.add(sub)
    await db.flush()

    provider = FakeProvider()
    provider.orders["lclw_order_2"] = {
        "amount": 700,
        "currency": "EUR",
        "type": "payment",
        "completed_at": "2026-01-01T00:00:00+00:00",
        "channel_data": {"subscription_id": "psub_lclw", "subscription_cycle_id": "cycle_2"},
    }
    mgr = PaymentManager(provider, db)

    with pytest.raises(RuntimeError, match=str(account_id)):
        await mgr.handle_event(
            PaymentEvent(
                provider="fake",
                type=PaymentEventType.order_completed,
                provider_event_id="ORDER_COMPLETED:lclw_2",
                provider_subscription_id="psub_lclw",
                order_id="lclw_order_2",
            )
        )


@pytest.mark.asyncio
async def test_lclw_echo_arm1_already_terminal_writes_no_state_but_records_the_event(db):
    """provider_cancelled already set: the echo changes no state, but is recorded with its
    provider event id so a redelivery dedups instead of being re-evaluated."""
    account_id = uuid.uuid4()
    sub = _lclw_sub(
        account_id,
        status="active",
        current_period_end=datetime.now() + timedelta(days=10),
        provider_cancelled=True,
    )
    db.add(sub)
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    event = PaymentEvent(
        provider="fake",
        type=PaymentEventType.subscription_cancelled,
        provider_event_id="SUBSCRIPTION_CANCELLED:1",
        provider_subscription_id="psub_lclw",
    )
    await mgr.handle_event(event)

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "active"
    assert refreshed.cancel_at_period_end is False
    assert await _event_types(db, sub.id) == ["provider_cancel_confirmed"]

    # The redelivery dedups on the recorded provider event id: still exactly one event.
    await mgr.handle_event(event)
    assert await _event_types(db, sub.id) == ["provider_cancel_confirmed"]


@pytest.mark.asyncio
async def test_lclw_echo_arm2_wind_down_while_cycle_is_running(db):
    """Cycle still running: the echo schedules a wind-down (cancel_at_period_end +
    provider_cancelled), regardless of whether cancel_at_period_end was already flagged."""
    account_id = uuid.uuid4()
    sub = _lclw_sub(
        account_id,
        status="active",
        current_period_end=datetime.now() + timedelta(days=10),
    )
    db.add(sub)
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.subscription_cancelled,
            provider_event_id="SUBSCRIPTION_CANCELLED:2",
            provider_subscription_id="psub_lclw",
        )
    )

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "active"  # still entitled for the rest of the cycle
    assert refreshed.cancel_at_period_end is True
    assert refreshed.provider_cancelled is True
    assert "cancelled" not in await _event_types(db, sub.id)
    assert await _event_types(db, sub.id) == ["provider_cancel_confirmed"]


@pytest.mark.asyncio
async def test_lclw_echo_arm2_wind_down_when_flag_already_set(db):
    """cancel_at_period_end already True (e.g. a prior downgrade-to-free request): the echo
    still lands in arm 2 (cycle running) and sets provider_cancelled, not a terminal cancel."""
    account_id = uuid.uuid4()
    sub = _lclw_sub(
        account_id,
        status="active",
        current_period_end=datetime.now() + timedelta(days=10),
        cancel_at_period_end=True,
    )
    db.add(sub)
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.subscription_cancelled,
            provider_event_id="SUBSCRIPTION_CANCELLED:2b",
            provider_subscription_id="psub_lclw",
        )
    )

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "active"
    assert refreshed.cancel_at_period_end is True
    assert refreshed.provider_cancelled is True
    assert "cancelled" not in await _event_types(db, sub.id)


@pytest.mark.asyncio
async def test_lclw_cancel_echo_on_superseded_row_is_a_noop_when_new_row_is_live(db):
    """Regression: ``_supersede_other_subs`` cancels the OLD row at the provider without ever
    setting ``provider_cancelled``. The provider's echo of that cancel must not re-terminate an
    already-``cancelled`` row nor stomp lc_users.tier while a fresh row is live for the account."""
    account_id = uuid.uuid4()
    await _lc_user(db, account_id, tier="pro")
    old_sub = _lclw_sub(
        account_id,
        status="cancelled",
        tier="starter",
        provider_subscription_id="psub_old",
    )
    new_sub = _lclw_sub(
        account_id,
        status="active",
        tier="pro",
        provider_subscription_id="psub_new",
        current_period_end=datetime.now() + timedelta(days=20),
    )
    db.add_all([old_sub, new_sub])
    await db.flush()
    db.add(PlanSubscriptionEvent(subscription_id=old_sub.id, event_type="cancelled_for_upgrade"))
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.subscription_cancelled,
            provider_event_id="SUBSCRIPTION_CANCELLED:superseded",
            provider_subscription_id="psub_old",
        )
    )

    refreshed_old = await db.get(PlanSubscription, old_sub.id)
    assert refreshed_old.status == "cancelled"
    types = await _event_types(db, old_sub.id)
    assert "cancelled" not in types  # no SECOND terminal event on top of the supersede's own
    assert types.count("provider_cancel_confirmed") == 1

    lc_user = (
        await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
    ).scalar_one()
    assert lc_user.tier == "pro"  # the new live row's tier, untouched


@pytest.mark.asyncio
async def test_lclw_cancel_echo_on_retired_checkout_is_a_noop_when_new_row_is_live(db):
    """Regression: an abandoned checkout retired via ``_record_checkout_retired`` (status
    "expired", never carried ``provider_cancelled``) must not be re-terminated by a delayed
    provider echo, nor overwrite the tier of an unrelated live row on the same account."""
    account_id = uuid.uuid4()
    await _lc_user(db, account_id, tier="starter")
    checkout = _lclw_sub(
        account_id,
        status="expired",
        provider_subscription_id="psub_checkout",
        current_period_start=None,
        current_period_end=None,
    )
    live = _lclw_sub(
        account_id,
        status="active",
        tier="starter",
        provider_subscription_id="psub_live",
        current_period_end=datetime.now() + timedelta(days=20),
    )
    db.add_all([checkout, live])
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.subscription_cancelled,
            provider_event_id="SUBSCRIPTION_CANCELLED:retired_checkout",
            provider_subscription_id="psub_checkout",
        )
    )

    refreshed_checkout = await db.get(PlanSubscription, checkout.id)
    assert refreshed_checkout.status == "expired"
    assert await _event_types(db, checkout.id) == ["provider_cancel_confirmed"]

    lc_user = (
        await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
    ).scalar_one()
    assert lc_user.tier == "starter"  # untouched by the retired checkout's echo


@pytest.mark.asyncio
async def test_lclw_echo_arm3_terminal_cancel_syncs_tier_to_free(db):
    """Cycle already over (or unknown): the row is terminally cancelled and lc_users.tier
    drops to free."""
    account_id = uuid.uuid4()
    await _lc_user(db, account_id, tier="starter")
    sub = _lclw_sub(
        account_id,
        status="active",
        current_period_end=datetime.now() - timedelta(hours=1),
    )
    db.add(sub)
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.subscription_cancelled,
            provider_event_id="SUBSCRIPTION_CANCELLED:3",
            provider_subscription_id="psub_lclw",
        )
    )

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "cancelled"
    assert "cancelled" in await _event_types(db, sub.id)

    lc_user = (
        await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
    ).scalar_one()
    assert lc_user.tier == "free"


@pytest.mark.asyncio
async def test_lclw_refund_metadata_carries_related_order_id(db):
    """Refund classification stamps ``related_order_id`` off the cached order dict."""
    account_id = uuid.uuid4()
    await _lc_user(db, account_id, tier="starter")
    sub = _lclw_sub(account_id, status="active", provider_subscription_id="psub_lclw_refund")
    db.add(sub)
    await db.flush()
    db.add(PlanSubscriptionEvent(subscription_id=sub.id, event_type="activated"))
    await db.flush()

    provider = FakeProvider()
    provider.orders["lclw_refund_1"] = {
        "type": "refund",
        "state": "completed",
        "related_order_id": "lclw_order_1",
    }
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:lclw_refund_1",
            provider_subscription_id="psub_lclw_refund",
            order_id="lclw_refund_1",
        )
    )

    event = (
        await db.execute(
            select(PlanSubscriptionEvent).where(
                PlanSubscriptionEvent.subscription_id == sub.id,
                PlanSubscriptionEvent.event_type == "refunded",
            )
        )
    ).scalar_one()
    assert event.metadata_json["related_order_id"] == "lclw_order_1"
