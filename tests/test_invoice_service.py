"""Invoice issuance: numbering, idempotency, VAT math, buyer snapshot."""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest

from src.models.user import User
from src.models.user_billing_details import UserBillingDetails
from src.services.invoice import issue_invoice


async def _user(db, email=None):
    user = User(email=email or f"svc-{uuid.uuid4().hex[:8]}@test.io")
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_eur_vat_back_calculation(db):
    user = await _user(db)
    inv = await issue_invoice(
        db,
        user_id=user.id,
        user_email=user.email,
        external_reference=f"revolut:{uuid.uuid4().hex[:8]}",
        gross_minor=2000,
        currency="EUR",
        tax_minor=None,
        payment_date=datetime(2026, 8, 1),
        line_label="Prepaid credits",
    )
    assert inv.gross_amount == Decimal("20.00")
    assert inv.net_amount == Decimal("16.67")
    assert inv.vat_amount == Decimal("3.33")  # gross - net, totals always exact
    assert inv.vat_rate == Decimal("0.2000")
    await db.commit()


@pytest.mark.asyncio
async def test_provider_tax_amount_preferred(db):
    user = await _user(db)
    inv = await issue_invoice(
        db,
        user_id=user.id,
        user_email=user.email,
        external_reference=f"revolut:{uuid.uuid4().hex[:8]}",
        gross_minor=2000,
        currency="EUR",
        tax_minor=334,  # provider says 3.34 — trust it over back-calculation
        payment_date=datetime(2026, 8, 1),
        line_label="Prepaid credits",
    )
    assert inv.vat_amount == Decimal("3.34")
    assert inv.net_amount == Decimal("16.66")
    await db.commit()


@pytest.mark.asyncio
async def test_usd_no_vat(db):
    user = await _user(db)
    inv = await issue_invoice(
        db,
        user_id=user.id,
        user_email=user.email,
        external_reference=f"revolut:{uuid.uuid4().hex[:8]}",
        gross_minor=5000,
        currency="USD",
        tax_minor=None,
        payment_date=datetime(2026, 8, 1),
        line_label="Prepaid credits",
    )
    assert inv.vat_amount == Decimal("0.00")
    assert inv.net_amount == Decimal("50.00")
    assert inv.vat_rate == Decimal("0.0000")
    await db.commit()


@pytest.mark.asyncio
async def test_idempotent_on_external_reference(db):
    user = await _user(db)
    ref = f"revolut:{uuid.uuid4().hex[:8]}"
    kwargs = {
        "user_id": user.id,
        "user_email": user.email,
        "external_reference": ref,
        "gross_minor": 2000,
        "currency": "EUR",
        "tax_minor": None,
        "payment_date": datetime(2026, 8, 1),
        "line_label": "Prepaid credits",
    }
    first = await issue_invoice(db, **kwargs)
    await db.commit()
    second = await issue_invoice(db, **kwargs)
    assert first is not None and second is None
    await db.commit()


@pytest.mark.asyncio
async def test_zero_amount_skipped(db):
    user = await _user(db)
    inv = await issue_invoice(
        db,
        user_id=user.id,
        user_email=user.email,
        external_reference=f"revolut:{uuid.uuid4().hex[:8]}",
        gross_minor=0,
        currency="EUR",
        tax_minor=None,
        payment_date=datetime(2026, 8, 1),
        line_label="x",
    )
    assert inv is None


@pytest.mark.asyncio
async def test_sequence_is_per_year_and_contiguous(db):
    user = await _user(db)
    made = []
    for _ in range(3):
        inv = await issue_invoice(
            db,
            user_id=user.id,
            user_email=user.email,
            external_reference=f"revolut:{uuid.uuid4().hex[:8]}",
            gross_minor=1000,
            currency="USD",
            tax_minor=None,
            payment_date=datetime(2026, 8, 1),
            line_label="Prepaid credits",
        )
        await db.commit()
        made.append(inv)
    seqs = [i.seq for i in made]
    assert seqs == list(range(seqs[0], seqs[0] + 3))
    assert all(i.number == f"LTAI-{i.year}-{i.seq:04d}" for i in made)


@pytest.mark.asyncio
async def test_buyer_snapshot_includes_billing_details(db):
    user = await _user(db)
    db.add(UserBillingDetails(user_id=user.id, name="ACME SARL", country="France", vat_number="FR123"))
    await db.flush()
    inv = await issue_invoice(
        db,
        user_id=user.id,
        user_email=user.email,
        external_reference=f"revolut:{uuid.uuid4().hex[:8]}",
        gross_minor=2000,
        currency="EUR",
        tax_minor=None,
        payment_date=datetime(2026, 8, 1),
        line_label="Prepaid credits",
    )
    assert inv.buyer["email"] == user.email
    assert inv.buyer["name"] == "ACME SARL"
    await db.commit()
