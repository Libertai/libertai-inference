"""Invoice/billing-details model constraints."""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from src.models.invoice import Invoice
from src.models.user import User
from src.models.user_billing_details import UserBillingDetails


def _invoice(user_id, *, seq=1, ref=None):
    return Invoice(
        number=f"LTAI-2026-{seq:04d}",
        year=2026,
        seq=seq,
        user_id=user_id,
        issued_at=datetime(2026, 8, 24),
        payment_date=datetime(2026, 8, 1),
        external_reference=ref or f"revolut:ord_{uuid.uuid4().hex[:8]}",
        currency="EUR",
        net_amount=Decimal("16.67"),
        vat_amount=Decimal("3.33"),
        gross_amount=Decimal("20.00"),
        vat_rate=Decimal("0.2000"),
        line_label="Prepaid credits",
        seller={"legal_name": "INTELLIGENCE ARTIFICIELLE GENERALE"},
        buyer={"email": "a@b.c"},
    )


@pytest.mark.asyncio
async def test_user_delete_blocked_while_invoices_exist(db):
    user = User(email=f"inv-{uuid.uuid4().hex[:8]}@test.io")
    db.add(user)
    await db.flush()
    db.add(_invoice(user.id))
    await db.flush()
    with pytest.raises(IntegrityError):
        await db.execute(delete(User).where(User.id == user.id))
    await db.rollback()


@pytest.mark.asyncio
async def test_duplicate_external_reference_rejected(db):
    user = User(email=f"inv-{uuid.uuid4().hex[:8]}@test.io")
    db.add(user)
    await db.flush()
    db.add(_invoice(user.id, seq=1, ref="revolut:dup"))
    await db.flush()
    db.add(_invoice(user.id, seq=2, ref="revolut:dup"))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_billing_details_roundtrip(db):
    user = User(email=f"inv-{uuid.uuid4().hex[:8]}@test.io")
    db.add(user)
    await db.flush()
    db.add(UserBillingDetails(user_id=user.id, name="ACME SARL", vat_number="FR123"))
    await db.flush()
    row = (await db.execute(select(UserBillingDetails).where(UserBillingDetails.user_id == user.id))).scalar_one()
    assert row.as_snapshot()["name"] == "ACME SARL"
