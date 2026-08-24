"""Invoice issuance: per-year gap-free numbering + immutable snapshots.

``issue_invoice`` must run inside the caller's transaction as its LAST DB work:
it takes ``pg_advisory_xact_lock(INVOICE_NUMBER_LOCK_CLASS, year)``, which is held
to commit and globally serializes numbering — never hold it across provider I/O.
"""

import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.invoice import Invoice
from src.models.user_billing_details import UserBillingDetails
from src.services.geo import vat_rate_for_currency
from src.utils.logger import setup_logger
from src.utils.pg_locks import INVOICE_NUMBER_LOCK_CLASS

logger = setup_logger(__name__)

# Series prefix is per-product: LiberClaw's port uses its own series ("LCLW").
INVOICE_NUMBER_PREFIX = "LTAI"

SELLER_SNAPSHOT: dict = {
    "legal_name": "INTELLIGENCE ARTIFICIELLE GENERALE",
    "trade_name": "LibertAI",
    "legal_form": "SAS au capital de 10 000 €",
    "rcs": "RCS Créteil 985 392 356",
    "siren": "985392356",
    "siret": "98539235600012",
    "address_line1": "76 Promenade des Anglais",
    "postal_code": "94210",
    "city": "Saint-Maur-des-Fossés",
    "country": "France",
    "vat_number": "FR36985392356",
}


def _minor_to_decimal(minor: int) -> Decimal:
    return (Decimal(minor) / 100).quantize(Decimal("0.01"))


async def issue_invoice(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    user_email: str,
    external_reference: str,
    gross_minor: int,
    currency: str,
    tax_minor: int | None,
    payment_date: datetime,
    line_label: str,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> Invoice | None:
    if gross_minor <= 0:
        return None

    year = datetime.now(timezone.utc).year
    await db.execute(select(func.pg_advisory_xact_lock(INVOICE_NUMBER_LOCK_CLASS, year)))

    # Under the year lock a concurrent issuance has either committed (visible here)
    # or is queued behind us, so this read is conclusive and no number is burnt.
    existing = (
        await db.execute(select(Invoice.id).where(Invoice.external_reference == external_reference))
    ).scalar_one_or_none()
    if existing is not None:
        return None

    gross = _minor_to_decimal(gross_minor)
    vat_rate = Decimal(str(vat_rate_for_currency(currency)))
    if tax_minor is not None:
        vat = _minor_to_decimal(tax_minor)
    elif vat_rate > 0:
        net = (gross / (1 + vat_rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat = gross - net
    else:
        vat = Decimal("0.00")
    net = gross - vat

    billing = (
        await db.execute(select(UserBillingDetails).where(UserBillingDetails.user_id == user_id))
    ).scalar_one_or_none()
    buyer: dict = {"email": user_email}
    if billing is not None:
        buyer.update({k: v for k, v in billing.as_snapshot().items() if v})

    max_seq = (
        await db.execute(select(func.coalesce(func.max(Invoice.seq), 0)).where(Invoice.year == year))
    ).scalar_one()
    seq = max_seq + 1

    invoice = Invoice(
        number=f"{INVOICE_NUMBER_PREFIX}-{year}-{seq:04d}",
        year=year,
        seq=seq,
        user_id=user_id,
        payment_date=payment_date,
        external_reference=external_reference,
        currency=currency,
        net_amount=net,
        vat_amount=vat,
        gross_amount=gross,
        vat_rate=vat_rate.quantize(Decimal("0.0001")),
        line_label=line_label,
        period_start=period_start,
        period_end=period_end,
        seller=SELLER_SNAPSHOT,
        buyer=buyer,
    )
    db.add(invoice)
    await db.flush()
    logger.info(f"Issued invoice {invoice.number} for {external_reference}")
    return invoice
