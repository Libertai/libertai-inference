"""$1 LiberClaw agent-verification orders: checkout reuse and webhook settlement."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.config import config
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


def _order_with_attempts(*attempts: tuple[str, str]) -> dict:
    """Order payload with (state, fingerprint) payment attempts in delivery order."""
    return {
        "amount": 100,
        "currency": "USD",
        "payments": [
            {"state": state, "payment_method": {"fingerprint": fingerprint}} for state, fingerprint in attempts
        ],
    }


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
async def test_cancelled_order_marks_failed_without_grant(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    await _pending_row(db, account_id, "order-1")
    mgr = PaymentManager(FakeProvider(), db)

    await mgr.handle_event(_event("order-1", PaymentEventType.order_cancelled))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "failed"
    assert await _grants(db, account_id) == []


@pytest.mark.asyncio
async def test_expiry_marks_old_pending_failed_and_keeps_recent(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    old = await _pending_row(db, account_id, "order-old")
    await _pending_row(db, account_id, "order-new")
    old.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=31)
    await db.flush()
    mgr = PaymentManager(FakeProvider(), db)

    expired = await mgr.expire_stale_agent_verifications()

    assert expired == 1
    rows = {
        row.provider_order_id: row.status
        for row in (
            await db.execute(
                select(LiberclawAgentVerification).where(LiberclawAgentVerification.liberclaw_account_id == account_id)
            )
        )
        .scalars()
        .all()
    }
    assert rows == {"order-old": "failed", "order-new": "pending"}


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


@pytest.mark.asyncio
async def test_status_reports_older_paid_order_shadowed_by_newer_pending(db):
    from src.services.liberclaw_agent_verification import get_agent_verification_status

    account_id = uuid.uuid4()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    paid = LiberclawAgentVerification(
        liberclaw_account_id=account_id,
        provider="fake",
        provider_order_id=f"paid-{account_id.hex}",
        amount_cents=100,
        currency="USD",
        status="paid",
    )
    newer = LiberclawAgentVerification(
        liberclaw_account_id=account_id,
        provider="fake",
        provider_order_id=f"newer-{account_id.hex}",
        amount_cents=100,
        currency="USD",
        status="pending",
    )
    db.add_all([paid, newer])
    await db.flush()
    paid.created_at = now - timedelta(minutes=45)
    paid.paid_at = now - timedelta(minutes=40)
    newer.created_at = now - timedelta(minutes=1)
    await db.flush()

    response = await get_agent_verification_status(db, account_id)

    assert response.verified is True
    assert response.status == "paid"
    assert response.order_id == f"paid-{account_id.hex}"


@pytest.mark.asyncio
async def test_status_falls_back_to_the_latest_pending_order(db):
    from src.services.liberclaw_agent_verification import get_agent_verification_status

    account_id = uuid.uuid4()
    await _pending_row(db, account_id, f"pending-{account_id.hex}")

    response = await get_agent_verification_status(db, account_id)

    assert response.verified is False
    assert response.status == "pending"
    assert response.order_id == f"pending-{account_id.hex}"


@pytest.mark.asyncio
async def test_currency_mismatch_is_not_accepted(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    await _pending_row(db, account_id, "order-1")
    provider = FakeProvider()
    provider.orders["order-1"] = _completed_order(currency="EUR")
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
async def test_same_account_second_payment_keeps_one_fingerprint(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    db.add(
        LiberclawAgentVerification(
            liberclaw_account_id=account_id,
            provider="fake",
            provider_order_id="order-1",
            amount_cents=100,
            currency="USD",
            status="paid",
            payment_method_fingerprint="same-card",
        )
    )
    await _pending_row(db, account_id, "order-2")
    provider = FakeProvider()
    provider.orders["order-2"] = _completed_order(fingerprint="same-card")
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-2"))

    second = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-2")
        )
    ).scalar_one()
    assert second.status == "paid"
    assert second.payment_method_fingerprint is None
    assert len(await _grants(db, account_id)) == 1


@pytest.mark.asyncio
async def test_paid_uncredited_redelivery_grants_credit(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    paid = LiberclawAgentVerification(
        liberclaw_account_id=account_id,
        provider="fake",
        provider_order_id="order-1",
        amount_cents=100,
        currency="USD",
        status="paid",
    )
    paid.paid_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(paid)
    provider = FakeProvider()
    provider.orders["order-1"] = _completed_order()
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-1"))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "paid"
    assert row.credited_at is not None
    assert len(await _grants(db, account_id)) == 1


@pytest.mark.asyncio
async def test_fingerprint_comes_from_the_capturing_payment(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    await _pending_row(db, account_id, "order-1")
    provider = FakeProvider()
    provider.orders["order-1"] = _order_with_attempts(("declined", "fp-declined"), ("completed", "fp-paid"))
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-1"))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "paid"
    assert row.payment_method_fingerprint == "fp-paid"


@pytest.mark.asyncio
async def test_declined_card_does_not_falsely_conflict_with_a_bound_card(db):
    """A declined attempt must not bind its card: only the card that captured counts."""
    other_account = uuid.uuid4()
    account_id = uuid.uuid4()
    await _lc_account(db, other_account)
    await _lc_account(db, account_id)
    db.add(
        LiberclawAgentVerification(
            liberclaw_account_id=other_account,
            provider="fake",
            provider_order_id="other-order",
            amount_cents=100,
            currency="USD",
            status="paid",
            payment_method_fingerprint="fp-declined",
        )
    )
    await _pending_row(db, account_id, "order-1")
    provider = FakeProvider()
    provider.orders["order-1"] = _order_with_attempts(("declined", "fp-declined"), ("completed", "fp-paid"))
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-1"))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "paid"
    assert row.payment_method_fingerprint == "fp-paid"
    assert len(await _grants(db, account_id)) == 1


@pytest.mark.asyncio
async def test_late_completion_after_expiry_still_verifies_and_grants(db):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    row = await _pending_row(db, account_id, "order-late")
    row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=31)
    await db.flush()
    provider = FakeProvider()
    provider.orders["order-late"] = _completed_order()
    mgr = PaymentManager(provider, db)

    assert await mgr.expire_stale_agent_verifications() == 1
    assert row.status == "failed"
    await mgr.handle_event(_event("order-late"))

    refreshed = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-late")
        )
    ).scalar_one()
    assert refreshed.status == "paid"
    assert refreshed.credited_at is not None
    assert len(await _grants(db, account_id)) == 1


@pytest.mark.asyncio
async def test_settles_verification_while_billing_flag_is_off(db, monkeypatch):
    """Verification is not a subscription: the cutover flag must not block its settlement."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    await _pending_row(db, account_id, "order-1")
    provider = FakeProvider()
    provider.orders["order-1"] = _completed_order()
    mgr = PaymentManager(provider, db)

    await mgr.handle_event(_event("order-1"))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "paid"
    assert len(await _grants(db, account_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", [PaymentEventType.order_failed, PaymentEventType.order_cancelled])
async def test_late_failure_does_not_clobber_a_paid_row(db, event_type):
    account_id = uuid.uuid4()
    await _lc_account(db, account_id)
    db.add(
        LiberclawAgentVerification(
            liberclaw_account_id=account_id,
            provider="fake",
            provider_order_id="order-1",
            amount_cents=100,
            currency="USD",
            status="paid",
        )
    )
    await db.flush()
    mgr = PaymentManager(FakeProvider(), db)

    await mgr.handle_event(_event("order-1", event_type))

    row = (
        await db.execute(
            select(LiberclawAgentVerification).where(LiberclawAgentVerification.provider_order_id == "order-1")
        )
    ).scalar_one()
    assert row.status == "paid"


@pytest.mark.asyncio
async def test_conflicted_account_can_retry_and_verify_with_a_new_card(db):
    """A conflict is not paid, so checkout re-opens and a different card verifies."""
    other_account = uuid.uuid4()
    account_id = uuid.uuid4()
    await _lc_account(db, other_account)
    await _lc_account(db, account_id)
    db.add(
        LiberclawAgentVerification(
            liberclaw_account_id=other_account,
            provider="fake",
            provider_order_id="other-order",
            amount_cents=100,
            currency="USD",
            status="paid",
            payment_method_fingerprint="fp-taken",
        )
    )
    db.add(
        LiberclawAgentVerification(
            liberclaw_account_id=account_id,
            provider="fake",
            provider_order_id="conflicted-order",
            amount_cents=100,
            currency="USD",
            status="conflict",
        )
    )
    await db.flush()
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    result = await mgr.start_agent_verification(account_id, "a@example.com", "https://app.example/return")

    assert result is not None
    assert provider.topups == [(1.0, "USD")]
    provider.orders[result.order_id] = _completed_order(fingerprint="fp-new")
    await mgr.handle_event(_event(result.order_id))

    rows = {
        row.provider_order_id: row
        for row in (
            await db.execute(
                select(LiberclawAgentVerification).where(LiberclawAgentVerification.liberclaw_account_id == account_id)
            )
        )
        .scalars()
        .all()
    }
    assert rows["conflicted-order"].status == "conflict"
    assert rows[result.order_id].status == "paid"
    assert rows[result.order_id].payment_method_fingerprint == "fp-new"
    assert len(await _grants(db, account_id)) == 1
