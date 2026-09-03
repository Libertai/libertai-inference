"""Pure-part tests for the Revolut-vs-archive reconcile script: order classification and
the three invariant queries, seeded against real Invoice rows.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from scripts import reconcile_invoices
from scripts.reconcile_invoices import (
    check_lclw_cycle_uniqueness,
    check_orders_have_matching_invoice,
    check_series_sequence_gapless,
    classify_order,
)
from src.models.base import AsyncSessionLocal
from src.models.invoice import Invoice
from src.services.invoice import SERIES_LCLW, issue_invoice


async def _cleanup(*refs: str):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Invoice).where(Invoice.external_reference.in_(refs)))
        await db.commit()


async def _seed(ref: str, **overrides) -> Invoice:
    kwargs = {
        "liberclaw_account_id": uuid.uuid4(),
        "user_email": "reconcile@example.com",
        "external_reference": ref,
        "gross_minor": 1200,
        "currency": "EUR",
        "tax_minor": None,
        "payment_date": datetime(2026, 1, 1),
        "line_label": "LiberClaw Pro subscription",
        "series": SERIES_LCLW,
    }
    kwargs.update(overrides)
    async with AsyncSessionLocal() as db:
        invoice = await issue_invoice(db, **kwargs)
        await db.commit()
    assert invoice is not None
    return invoice


# --------------------------------------------------------------------- classify_order


def test_classify_own_topup():
    order = {"merchant_order_ext_ref": "topup:abc", "channel_data": {}}
    assert classify_order(order, own_subscription_ids=set()) == "own"


def test_classify_own_subscription():
    order = {"channel_data": {"subscription_id": "psub_own"}}
    assert classify_order(order, own_subscription_ids={"psub_own"}) == "own"


def test_classify_liberclaw_positive():
    order = {"channel_data": {"subscription_id": "lc_sub_1"}, "description": "LiberClaw Pro #3"}
    assert classify_order(order, own_subscription_ids=set()) == "liberclaw"


def test_classify_unclassifiable_without_description_corroboration():
    order = {"channel_data": {"subscription_id": "lc_sub_1"}, "description": "Something else"}
    assert classify_order(order, own_subscription_ids=set()) == "unclassifiable"


def test_classify_unclassifiable_no_subscription_id():
    order = {"channel_data": {}}
    assert classify_order(order, own_subscription_ids=set()) == "unclassifiable"


# --------------------------------------------------------------------- invariant 1: gap-free sequence


async def test_sequence_gapless_detects_gap():
    ref_a = f"revolut:{uuid.uuid4().hex}"
    ref_b = f"revolut:{uuid.uuid4().hex}"
    try:
        inv_a = await _seed(ref_a)
        await _seed(ref_b)  # seq = inv_a.seq + 1, becomes the new max after inv_a is removed

        async with AsyncSessionLocal() as db:
            await db.execute(delete(Invoice).where(Invoice.id == inv_a.id))
            await db.commit()

        async with AsyncSessionLocal() as db:
            violations = await check_series_sequence_gapless(db)
        assert any(v.startswith(f"{SERIES_LCLW}-2026") for v in violations)
    finally:
        await _cleanup(ref_a, ref_b)


async def test_sequence_gapless_clean_when_contiguous():
    ref = f"revolut:{uuid.uuid4().hex}"
    try:
        await _seed(ref)
        async with AsyncSessionLocal() as db:
            violations = await check_series_sequence_gapless(db)
        assert not any(v.startswith(f"{SERIES_LCLW}-2026") for v in violations)
    finally:
        await _cleanup(ref)


# --------------------------------------------------------------------- invariant 2: LCLW cycle uniqueness


async def test_lclw_cycle_uniqueness_rejected_at_insert():
    """uq_invoices_lclw_sub_cycle (a DB-level partial unique index) now enforces this
    invariant directly, so a duplicate (provider_subscription_id, cycle_id) can no longer
    reach the table for check_lclw_cycle_uniqueness to find — the second insert fails."""
    sub_id = f"psub_{uuid.uuid4().hex}"
    cycle_id = "cyc_1"
    ref_a = f"revolut:{uuid.uuid4().hex}"
    ref_b = f"revolut:{uuid.uuid4().hex}"
    try:
        await _seed(ref_a, provider_subscription_id=sub_id, cycle_id=cycle_id)
        with pytest.raises(IntegrityError):
            await _seed(ref_b, provider_subscription_id=sub_id, cycle_id=cycle_id)
    finally:
        await _cleanup(ref_a, ref_b)


async def test_lclw_cycle_uniqueness_clean_for_distinct_cycles():
    sub_id = f"psub_{uuid.uuid4().hex}"
    ref_a = f"revolut:{uuid.uuid4().hex}"
    ref_b = f"revolut:{uuid.uuid4().hex}"
    try:
        await _seed(ref_a, provider_subscription_id=sub_id, cycle_id="cyc_1")
        await _seed(ref_b, provider_subscription_id=sub_id, cycle_id="cyc_2")

        async with AsyncSessionLocal() as db:
            violations = await check_lclw_cycle_uniqueness(db)
        assert not any(sub_id in v for v in violations)
    finally:
        await _cleanup(ref_a, ref_b)


# --------------------------------------------------------------------- invariant 3: order <-> invoice match


async def test_orders_have_matching_invoice_detects_missing():
    order = {"id": f"ord_{uuid.uuid4().hex}", "amount": 1200, "currency": "EUR"}
    async with AsyncSessionLocal() as db:
        violations = await check_orders_have_matching_invoice(db, [order])
    assert any(order["id"] in v and "no invoice found" in v for v in violations)


async def test_orders_have_matching_invoice_passes_when_gross_matches():
    order_id = f"ord_{uuid.uuid4().hex}"
    ref = f"revolut:{order_id}"
    order = {"id": order_id, "amount": 1200, "currency": "EUR"}
    try:
        await _seed(ref, gross_minor=1200)
        async with AsyncSessionLocal() as db:
            violations = await check_orders_have_matching_invoice(db, [order])
        assert violations == []
    finally:
        await _cleanup(ref)


async def test_orders_have_matching_invoice_detects_gross_mismatch():
    order_id = f"ord_{uuid.uuid4().hex}"
    ref = f"revolut:{order_id}"
    order = {"id": order_id, "amount": 5000, "currency": "EUR"}
    try:
        await _seed(ref, gross_minor=1200)
        async with AsyncSessionLocal() as db:
            violations = await check_orders_have_matching_invoice(db, [order])
        assert any("gross mismatch" in v for v in violations)
    finally:
        await _cleanup(ref)


async def test_orders_have_matching_invoice_skips_zero_amount_order():
    """issue_invoice no-ops on gross_minor <= 0, so a zero-amount completed order must
    never be flagged for lacking an invoice."""
    order = {"id": f"ord_{uuid.uuid4().hex}", "amount": 0, "currency": "EUR"}
    async with AsyncSessionLocal() as db:
        violations = await check_orders_have_matching_invoice(db, [order])
    assert violations == []


async def test_orders_have_matching_invoice_logs_and_skips_missing_amount(monkeypatch):
    """An order missing "amount" can't be checked either way -- it must be logged and
    skipped, not crash the run with a KeyError.

    reconcile_invoices' logger has propagate=False (see src/utils/logger.py), which
    keeps its records out of caplog's root-logger capture -- so this asserts on the
    logger call directly instead.
    """
    order = {"id": f"ord_{uuid.uuid4().hex}", "currency": "EUR"}
    logged: list[str] = []
    monkeypatch.setattr(reconcile_invoices.logger, "error", lambda msg: logged.append(msg))

    async with AsyncSessionLocal() as db:
        violations = await check_orders_have_matching_invoice(db, [order])

    assert violations == []
    assert any(order["id"] in msg for msg in logged)
