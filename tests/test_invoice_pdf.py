"""Invoice rendering: PDF/A-3b + Factur-X embed, mentions, escaping, store-once."""

import re
import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from facturx import get_facturx_xml_from_pdf
from lxml import etree

from src.models.invoice import Invoice
from src.services.invoice import SELLER_SNAPSHOT, issue_invoice
from src.services.invoice_pdf import TEMPLATE_VERSION, _render_html, get_or_render_pdf, render_invoice_pdf


def _invoice(**overrides):
    base = {
        "id": uuid.uuid4(),
        "number": "LTAI-2026-0042",
        "year": 2026,
        "seq": 42,
        "user_id": uuid.uuid4(),
        "issued_at": datetime(2026, 8, 24),
        "payment_date": datetime(2026, 8, 1),
        "external_reference": "revolut:test",
        "currency": "EUR",
        "net_amount": Decimal("16.67"),
        "vat_amount": Decimal("3.33"),
        "gross_amount": Decimal("20.00"),
        "vat_rate": Decimal("0.2000"),
        "line_label": "Prepaid credits",
        "period_start": None,
        "period_end": None,
        "seller": SELLER_SNAPSHOT,
        "buyer": {"email": "a@b.c"},
        "pdf": None,
        "template_version": None,
    }
    base.update(overrides)
    attrs = {k: base.pop(k) for k in ("id", "pdf", "template_version")}
    inv = Invoice(**base)
    for k, v in attrs.items():
        setattr(inv, k, v)
    return inv


def _pdf_text(pdf_bytes: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    return "".join(p.extract_text() for p in PdfReader(BytesIO(pdf_bytes)).pages)


def test_eur_invoice_contains_mandatory_mentions():
    text = _pdf_text(render_invoice_pdf(_invoice()))
    for needle in [
        "LTAI-2026-0042",
        "INTELLIGENCE ARTIFICIELLE GENERALE",
        "SAS au capital de 10 000",
        "RCS Créteil 985 392 356",
        "FR36985392356",
        "Prepaid credits",
        "16.67",
        "3.33",
        "20.00",
        "Escompte",
        "40 €",
        "Prestation de services",
    ]:
        assert re.sub(r"\s+", "", needle) in re.sub(r"\s+", "", text), needle
    assert "\u2014" not in text and "\u2013" not in text  # no em/en dashes on the document


def test_usd_invoice_has_259b_mention_and_no_vat_rate():
    text = _pdf_text(
        render_invoice_pdf(
            _invoice(
                currency="USD",
                net_amount=Decimal("50.00"),
                vat_amount=Decimal("0.00"),
                gross_amount=Decimal("50.00"),
                vat_rate=Decimal("0.0000"),
            )
        )
    )
    assert "259 B" in text
    assert "20%" not in text


def test_billing_details_rendered_and_escaped():
    inv = _invoice(
        buyer={
            "email": "a@b.c",
            "name": "<script>alert(1)</script> ACME",
            "address_line1": "12 Rue de la Paix",
            "postal_code": "75002",
            "city": "Paris",
            "country": "France",
        }
    )

    html = _render_html(inv)
    assert "&lt;script&gt;" in html  # Jinja autoescape: no raw markup reaches WeasyPrint
    assert "<script>" not in html

    text = _pdf_text(render_invoice_pdf(inv))
    assert "ACME" in text
    assert "RuedelaPaix" in text.replace(" ", "")
    assert "75002Paris".replace(" ", "") in text.replace(" ", "")


def test_facturx_xml_embedded_and_consistent():
    pdf = render_invoice_pdf(_invoice())
    _, xml_bytes = get_facturx_xml_from_pdf(pdf)
    root = etree.fromstring(xml_bytes)
    ns = {"ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"}
    assert root.findtext(".//ram:ID", namespaces=ns) is not None
    assert b"LTAI-2026-0042" in xml_bytes
    assert b"20.00" in xml_bytes


@pytest.mark.asyncio
async def test_get_or_render_stores_once(db):
    from src.models.user import User

    user = User(email=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    await db.flush()

    invoice = await issue_invoice(
        db,
        user_id=user.id,
        user_email=user.email,
        external_reference=f"revolut:{uuid.uuid4().hex}",
        gross_minor=2000,
        currency="EUR",
        tax_minor=333,
        payment_date=datetime(2026, 8, 1),
        line_label="Prepaid credits",
    )
    assert invoice is not None
    assert invoice.pdf is None
    assert invoice.template_version is None

    first = await get_or_render_pdf(db, invoice)
    assert invoice.pdf == first
    assert invoice.template_version == TEMPLATE_VERSION

    second = await get_or_render_pdf(db, invoice)
    assert second == first
