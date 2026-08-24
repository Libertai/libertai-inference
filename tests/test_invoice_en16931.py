"""EN 16931 business-rule (schematron) compliance of the generated CII XML.

Opt-in: needs `saxonche` installed and the official CII validation XSLT from
https://github.com/ConnectingEurope/eInvoicing-EN16931 (cii/xslt/EN16931-CII-validation.xslt),
pointed to by EN16931_CII_XSLT. Skipped otherwise — the XSLT is not vendored (size/license).
"""

import os
import re
from decimal import Decimal

import pytest

from src.services.invoice_pdf import build_cii_xml
from tests.test_invoice_pdf import _invoice

saxonche = pytest.importorskip("saxonche")

_XSLT = os.environ.get("EN16931_CII_XSLT", "")
pytestmark = pytest.mark.skipif(
    not _XSLT or not os.path.exists(_XSLT),
    reason="EN16931 schematron XSLT not available (set EN16931_CII_XSLT)",
)


def _failed_assertions(xml_bytes: bytes) -> list[str]:
    with saxonche.PySaxonProcessor(license=False) as proc:
        exe = proc.new_xslt30_processor().compile_stylesheet(stylesheet_file=_XSLT)
        node = proc.parse_xml(xml_text=xml_bytes.decode("utf-8"))
        svrl = exe.transform_to_string(xdm_node=node)
    return re.findall(r'<svrl:failed-assert[^>]*id="([^"]+)"', svrl)


def test_eur_invoice_passes_en16931_business_rules():
    inv = _invoice(
        buyer={
            "email": "a@b.c",
            "name": "ACME",
            "address_line1": "1 rue x",
            "postal_code": "75001",
            "city": "Paris",
            "country": "France",
        }
    )
    assert _failed_assertions(build_cii_xml(inv)) == []


def test_usd_invoice_passes_en16931_business_rules():
    inv = _invoice(
        currency="USD",
        net_amount=Decimal("50.00"),
        vat_amount=Decimal("0.00"),
        gross_amount=Decimal("50.00"),
        vat_rate=Decimal("0.0000"),
        buyer={
            "email": "a@b.c",
            "name": "ACME Corp",
            "address_line1": "1 Main St",
            "postal_code": "10001",
            "city": "New York",
            "country": "USA",
        },
    )
    assert _failed_assertions(build_cii_xml(inv)) == []


def test_email_only_buyer_fails_only_the_buyer_address_rules():
    """A consumer invoice has no buyer address to embed. That XML never reaches a PDP
    (B2C is e-reporting, not e-invoicing), so BR-10/BR-11 are the accepted ceiling —
    anything beyond them is a regression."""
    inv = _invoice()
    assert set(_failed_assertions(build_cii_xml(inv))) == {"BR-10", "BR-11"}
