"""$1 LiberClaw agent-verification orders: checkout reuse and webhook settlement."""

import uuid

import pytest
from sqlalchemy import select

from src.models.liberclaw_agent_verification import LiberclawAgentVerification
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.liberclaw_user import LiberclawUser
from src.services.payments.base import PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager
from tests.test_payment_manager import FakeProvider


async def _lc_account(db, account_id: uuid.UUID) -> LiberclawUser:
    lc_user = LiberclawUser(
        user_id=f"{account_id.hex}@example.com",
        user_type="email",
        liberclaw_account_id=account_id,
    )
    db.add(lc_user)
    await db.flush()
    return lc_user


def _event(order_id: str, event_type: PaymentEventType = PaymentEventType.order_completed) -> PaymentEvent:
    return PaymentEvent(
        provider="fake",
        type=event_type,
        provider_event_id=f"{event_type.value}:{order_id}",
        order_id=order_id,
    )


async def _pending_row(db, account_id: uuid.UUID, order_id: str) -> LiberclawAgentVerification:
    row = LiberclawAgentVerification(
        liberclaw_account_id=account_id,
        provider="fake",
        provider_order_id=order_id,
        checkout_url=f"https://checkout.example/{order_id}",
        amount_cents=100,
        currency="USD",
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row


def _completed_order(fingerprint: str | None = None, amount: int = 100, currency: str = "USD") -> dict:
    order: dict = {"amount": amount, "currency": currency}
    if fingerprint:
        order["payments"] = [{"payment_method": {"fingerprint": fingerprint}}]
    return order


async def _grants(db, account_id: uuid.UUID) -> list[LiberclawCreditGrant]:
    lc_user = (
        await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
    ).scalar_one()
    return list(
        (
            await db.execute(select(LiberclawCreditGrant).where(LiberclawCreditGrant.liberclaw_user_id == lc_user.id))
        ).scalars()
    )


@pytest.mark.asyncio
async def test_start_creates_one_dollar_order_and_reuses_pending(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    first = await mgr.start_agent_verification(account_id, "a@example.com", "https://app.example/return")
    second = await mgr.start_agent_verification(account_id, "a@example.com", "https://app.example/return")

    assert first is not None and second is not None
    assert first.order_id == second.order_id
    assert provider.topups == [(1.0, "USD")]
    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == first.order_id)
        )
    ).scalar_one()
    assert row.amount_cents == 100
    assert row.currency == "USD"
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_paid_account_is_not_charged_again(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    db.add(
        LiberclawAgentVerification(
            liberclaw_account_id=account_id,
            provider="fake",
            provider_order_id="paid-order",
            amount_cents=100,
            currency="USD",
            status="paid",
        )
    )
    await db.flush()
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    result = await mgr.start_agent_verification(account_id, "a@example.com", "https://app.example/return")

    assert result is None
    assert provider.topups == []


@pytest.mark.asyncio
async def test_completed_order_marks_paid_and_grants_one_dollar(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    await _pending_row(db, account_id, "order-1")
    provider = FakeProvider()
    provider.orders["order-1"] = _completed_order(fingerprint="fp-1")
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-1"))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "paid"
    assert row.paid_at is not None
    assert row.credited_at is not None
    assert row.payment_method_fingerprint == "fp-1"
    grants = await _grants(db, account_id)
    assert len(grants) == 1
    assert grants[0].amount == 1.0
    assert grants[0].external_reference == "agent-verification:order-1"


@pytest.mark.asyncio
async def test_duplicate_completion_grants_once(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    await _pending_row(db, account_id, "order-1")
    provider = FakeProvider()
    provider.orders["order-1"] = _completed_order()
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-1"))
    await mgr.handle_event(_event("order-1"))

    grants = await _grants(db, account_id)
    assert len(grants) == 1


@pytest.mark.asyncio
async def test_failed_order_marks_failed_without_grant(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    await _pending_row(db, account_id, "order-1")
    mgr = PaymentManager(FakeProvider(), db)

    await mgr.handle_event(_event("order-1", PaymentEventType.order_failed))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "failed"
    assert await _grants(db, account_id) == []


@pytest.mark.asyncio
async def test_amount_mismatch_is_not_accepted(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    await _pending_row(db, account_id, "order-1")
    provider = FakeProvider()
    provider.orders["order-1"] = _completed_order(amount=50)
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-1"))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "failed"
    assert await _grants(db, account_id) == []


@pytest.mark.asyncio
async def test_reused_card_verifies_only_one_account(db):
    first_account = uuid.uuid4()
    second_account = uuid.uuid4()
    await _lc_account(db, first_account)
    await _lc_account(db, second_account)
    db.add(
        LiberclawAgentVerification(
            liberclaw_account_id=first_account,
            provider="fake",
            provider_order_id="order-1",
            amount_cents=100,
            currency="USD",
            status="paid",
            payment_method_fingerprint="same-card",
        )
    )
    await _pending_row(db, second_account, "order-2")
    provider = FakeProvider()
    provider.orders["order-2"] = _completed_order(fingerprint="same-card")
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-2"))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-2")
        )
    ).scalar_one()
    assert row.status == "conflict"
    assert await _grants(db, second_account) == []


@pytest.mark.asyncio
async def test_unknown_order_is_ignored(db):
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("foreign-order"))

    assert provider.topups == []
