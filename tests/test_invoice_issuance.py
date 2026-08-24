"""Webhook-driven invoice issuance: scoped to locally-owned, non-refund, paid orders."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from src.interfaces.credits import CreditTransactionStatus
from src.models.credit_transaction import CreditTransaction
from src.models.invoice import Invoice
from src.services.payments.base import PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager, order_invoice_fields
from tests.test_payment_manager import FakeProvider, _active_plus_sub, _make_user


def _completed_event(order_id, **kwargs):
    return PaymentEvent(
        provider="fake",
        type=PaymentEventType.order_completed,
        provider_event_id=f"ORDER_COMPLETED:{order_id}",
        order_id=order_id,
        metadata={},
        **kwargs,
    )


async def _invoices(db) -> list[Invoice]:
    return list((await db.execute(select(Invoice))).scalars().all())


@pytest.mark.asyncio
async def test_topup_completion_issues_invoice(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    await mgr.start_topup(user, redirect_url="http://x", usd_credits=20.0, charge_amount=20.0, charge_currency="EUR")
    provider.orders["ord_1"] = {
        "amount": 2000,
        "currency": "EUR",
        "type": "payment",
        "completed_at": "2026-08-01T10:00:00+00:00",
        "line_items": [{"taxes": [{"amount": 333}]}],
    }

    await mgr.handle_event(_completed_event("ord_1"))

    invoices = await _invoices(db)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.user_id == user.id
    assert invoice.gross_amount == Decimal("20.00")
    assert invoice.vat_amount == Decimal("3.33")
    assert invoice.currency == "EUR"


@pytest.mark.asyncio
async def test_duplicate_webhook_single_invoice(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    await mgr.start_topup(user, redirect_url="http://x", usd_credits=10.0, charge_amount=10.0, charge_currency="USD")
    provider.orders["ord_1"] = {
        "amount": 1000,
        "currency": "USD",
        "type": "payment",
        "completed_at": "2026-08-01T10:00:00+00:00",
    }

    event = _completed_event("ord_1")
    await mgr.handle_event(event)
    await mgr.handle_event(event)  # replay

    assert len(await _invoices(db)) == 1


@pytest.mark.asyncio
async def test_foreign_order_no_invoice(db):
    """No local pending top-up and no subscription for this order: shared merchant account
    noise from another product, handled without touching invoices."""
    mgr = PaymentManager(FakeProvider(), db)

    await mgr.handle_event(_completed_event("someone_elses_order"))

    assert await _invoices(db) == []


@pytest.mark.asyncio
async def test_get_order_failure_fails_webhook(db):
    user = await _make_user(db)

    class FailingGetOrderProvider(FakeProvider):
        async def get_order(self, order_id: str) -> dict:
            raise RuntimeError("provider unreachable")

    user_id = user.id  # read before rollback expires the instance
    provider = FailingGetOrderProvider()
    mgr = PaymentManager(provider, db)
    await mgr.start_topup(user, redirect_url="http://x", usd_credits=10.0, charge_amount=10.0, charge_currency="USD")
    await db.commit()  # the pending row is durable; only the webhook's own work must roll back

    with pytest.raises(RuntimeError):
        await mgr.handle_event(_completed_event("ord_1"))
    # Mirrors the webhook route: an unhandled exception closes the session without a
    # commit, discarding whatever this transaction had flushed.
    await db.rollback()

    tx = (await db.execute(select(CreditTransaction).where(CreditTransaction.user_id == user_id))).scalar_one()
    assert tx.status == CreditTransactionStatus.pending  # same transaction: retry recovers both
    assert await _invoices(db) == []


@pytest.mark.asyncio
async def test_subscription_activation_issues_invoice_with_period(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    await mgr.start_checkout(user, tier="plus", redirect_url="http://x", currency="USD")
    provider.orders["setup_1"] = {
        "amount": 2000,
        "currency": "USD",
        "type": "payment",
        "completed_at": "2026-08-01T10:00:00+00:00",
    }

    await mgr.handle_event(_completed_event("setup_1", provider_subscription_id="psub_1"))

    invoices = await _invoices(db)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert "plus" in invoice.line_label
    assert invoice.period_start is not None
    assert invoice.period_end is not None


@pytest.mark.asyncio
async def test_order_failed_no_invoice(db):
    user = await _make_user(db)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)
    await mgr.start_topup(user, redirect_url="http://x", usd_credits=10.0, charge_amount=10.0, charge_currency="USD")

    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_failed,
            provider_event_id="ORDER_FAILED:ord_1",
            order_id="ord_1",
        )
    )

    assert await _invoices(db) == []


@pytest.mark.asyncio
async def test_refund_order_issues_no_invoice(db):
    """A refund settles as its own ORDER_COMPLETED event under a new order id (mirrors
    tests/test_payment_manager.py's test_refund_order_is_not_booked_as_a_renewal). It must not
    be booked as a paid renewal and must not issue an invoice."""
    provider = FakeProvider()
    _user, mgr = await _active_plus_sub(db, provider)
    before = len(await _invoices(db))

    provider.orders["ord_refund"] = {"type": "refund", "state": "completed"}
    await mgr.handle_event(_completed_event("ord_refund", provider_subscription_id="psub_1"))

    assert len(await _invoices(db)) == before


def test_order_invoice_fields_missing_amount_names_field_and_order():
    with pytest.raises(ValueError, match="ord_missing.*'amount'"):
        order_invoice_fields({"currency": "USD"}, "ord_missing")


def test_order_invoice_fields_missing_currency_names_field_and_order():
    with pytest.raises(ValueError, match="ord_missing.*'currency'"):
        order_invoice_fields({"amount": 100}, "ord_missing")
