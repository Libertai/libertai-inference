"""Render an invoice: Jinja HTML -> WeasyPrint PDF/A-3b -> Factur-X (EN 16931) embed.

Rendered once and stored: the retained document must stay byte-identical to what
the customer downloaded, across template and library upgrades.
"""

from pathlib import Path
from xml.sax.saxutils import escape

import anyio
from facturx import generate_from_binary
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from weasyprint import HTML

from src.models.invoice import Invoice
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

TEMPLATE_VERSION = 1

# LibertAI lockup (mark + wordmark), path data from the brand vector; rendered inline in the header.
LOGO_PATHS: list[str] = [
    "M824.805 137.893H802.647L829.517 11.1035H851.674L824.805 137.893Z",
    "M748.884 11.1035H772.089L793.2 137.893H770.694L766.68 111.486H713.99L699.334 137.893H675.432L748.884 11.1035ZM724.109 93.1251H763.889L755.515 36.8129L724.109 93.1251Z",
    "M639.864 18.9729H660.626V43.8057H679.468V58.8459H660.626V110.087C660.626 119.705 662.894 121.979 671.793 121.979H679.297V137.893H663.942C643.877 137.893 639.866 132.472 639.866 113.235V58.8459H625.907V43.8057H639.866V18.9729H639.864Z",
    "M619.885 64.968C616.397 64.6178 613.08 64.4437 609.942 64.4437C596.855 64.4437 589.354 69.6892 589.354 88.578V137.895H568.592V43.8077H589.004V60.597C594.064 50.1039 603.137 43.8077 614.478 43.6335C616.049 43.6335 618.318 43.8077 619.887 43.9838V64.97L619.885 64.968Z",
    "M481.531 96.7976C482.927 114.285 495.14 122.855 507.007 122.855C517.823 122.855 526.372 118.482 530.036 109.738H550.448C546.086 124.779 532.129 140.169 507.877 140.169C477.693 140.169 460.42 117.085 460.42 90.153C460.42 61.8218 479.788 41.7102 506.481 41.7102C535.272 41.7102 553.242 65.1441 551.497 96.7976H481.533H481.531ZM481.531 81.5813H530.384C529.861 66.8913 518.695 57.9713 506.481 57.9713C496.712 57.9713 483.276 63.7432 481.531 81.5813Z",
    "M379.954 11.1035V56.3982C385.537 47.1299 395.308 41.7083 407.87 41.7083C430.029 41.7083 448.698 60.7712 448.698 90.8496C448.698 121.104 430.031 140.167 407.87 140.167C395.308 140.167 385.537 134.745 379.954 125.477V137.893H359.192V11.1035H379.954ZM403.333 123.203C417.116 123.203 427.062 110.787 427.062 90.8496C427.062 71.0882 417.118 58.6718 403.333 58.6718C389.724 58.6718 379.081 69.1649 379.081 90.8496C379.081 112.534 389.724 123.203 403.333 123.203Z",
    "M341.671 11.1035V32.0897H320.91V11.1035H341.671ZM341.671 43.8077V137.895H320.91V43.8077H341.671Z",
    "M245.461 119.183H309.143V137.895H223.302V11.1035H245.459V119.181L245.461 119.183Z",
    "M106.511 0H56.8032V42.5633H106.511V92.3894H148.977V42.5633V0H106.511Z",
    "M99.2672 106.434L56.8032 148.999H106.511L148.977 106.434H99.2672Z",
    "M81.6561 106.434H42.7897V0L0.325684 42.5633V148.999H39.1921L81.6561 106.434Z",
]

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent.parent / "templates"),
    autoescape=select_autoescape(default=True, default_for_string=True),
)
_env.filters["fmt_amount"] = lambda v: f"{v:,.2f}"
_env.filters["fmt_date"] = lambda dt: dt.strftime("%b %d, %Y")
_env.filters["fmt_pct"] = lambda rate: f"{rate * 100:.2f}".rstrip("0").rstrip(".")


def _xml_escape(value: str) -> str:
    return escape(str(value))


# Buyer country is unvalidated free text (see UserBillingDetails); this maps the common
# spellings a buyer might type to the ISO 3166-1 alpha-2 code build_cii_xml's CountryID needs.
_COUNTRY_NAME_TO_ISO2: dict[str, str] = {
    "france": "FR",
    "germany": "DE",
    "allemagne": "DE",
    "belgium": "BE",
    "belgique": "BE",
    "spain": "ES",
    "espagne": "ES",
    "italy": "IT",
    "italie": "IT",
    "netherlands": "NL",
    "pays-bas": "NL",
    "luxembourg": "LU",
    "portugal": "PT",
    "ireland": "IE",
    "irlande": "IE",
    "austria": "AT",
    "autriche": "AT",
    "switzerland": "CH",
    "suisse": "CH",
    "united kingdom": "GB",
    "royaume-uni": "GB",
    "uk": "GB",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "états-unis": "US",
    "canada": "CA",
    "poland": "PL",
    "pologne": "PL",
    "sweden": "SE",
    "suède": "SE",
    "denmark": "DK",
    "danemark": "DK",
    "finland": "FI",
    "finlande": "FI",
    "greece": "GR",
    "grèce": "GR",
    "czech republic": "CZ",
    "czechia": "CZ",
    "république tchèque": "CZ",
    "bulgaria": "BG",
    "bulgarie": "BG",
    "croatia": "HR",
    "croatie": "HR",
    "hungary": "HU",
    "hongrie": "HU",
    "latvia": "LV",
    "lettonie": "LV",
    "lithuania": "LT",
    "lituanie": "LT",
    "malta": "MT",
    "malte": "MT",
    "romania": "RO",
    "roumanie": "RO",
    "slovakia": "SK",
    "slovaquie": "SK",
    "slovenia": "SI",
    "slovénie": "SI",
    "cyprus": "CY",
    "chypre": "CY",
    "estonia": "EE",
    "estonie": "EE",
    "norway": "NO",
    "norvège": "NO",
    "iceland": "IS",
    "japan": "JP",
    "japon": "JP",
    "australia": "AU",
    "australie": "AU",
    "new zealand": "NZ",
    "brazil": "BR",
    "brésil": "BR",
    "india": "IN",
    "inde": "IN",
    "singapore": "SG",
    "singapour": "SG",
    "south korea": "KR",
    "corée du sud": "KR",
    "mexico": "MX",
    "mexique": "MX",
    "argentina": "AR",
    "argentine": "AR",
    "turkey": "TR",
    "turquie": "TR",
    "israel": "IL",
    "israël": "IL",
    "united arab emirates": "AE",
    "émirats arabes unis": "AE",
}


def build_cii_xml(inv: Invoice) -> bytes:
    """EN 16931 CII. VAT category S (standard, EUR) or O (services outside scope, USD)."""
    # "20" or "19.6": trailing zeros stripped without Decimal.normalize()'s exponent form (2E+1).
    vat_pct = f"{(inv.vat_rate * 100):.2f}".rstrip("0").rstrip(".")
    if inv.currency == "EUR":
        category, rate = "S", str(vat_pct)
        tax_fragment = f"""
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>{inv.vat_amount}</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>{inv.net_amount}</ram:BasisAmount>
        <ram:CategoryCode>{category}</ram:CategoryCode>
        <ram:RateApplicablePercent>{rate}</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>"""
    else:
        # BR-O-05/-08: category O (outside scope) carries no VAT rate, at header or line.
        category, rate = "O", None
        tax_fragment = f"""
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>0.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:ExemptionReason>TVA non applicable - article 259 B du CGI</ram:ExemptionReason>
        <ram:BasisAmount>{inv.net_amount}</ram:BasisAmount>
        <ram:CategoryCode>{category}</ram:CategoryCode>
      </ram:ApplicableTradeTax>"""
    rate_xml = f"\n        <ram:RateApplicablePercent>{rate}</ram:RateApplicablePercent>" if rate is not None else ""
    # Line-level tax mirrors the header's single line item (one line per invoice here).
    line_tax_fragment = f"""
      <ram:ApplicableTradeTax>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:CategoryCode>{category}</ram:CategoryCode>{rate_xml}
      </ram:ApplicableTradeTax>"""

    # BR-O-02: an invoice whose lines are outside VAT scope (category O) must not carry
    # any VAT identifier in the structured data; the visual PDF still shows it.
    seller_vat_xml = (
        f'<ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{_xml_escape(inv.seller["vat_number"])}</ram:ID></ram:SpecifiedTaxRegistration>'
        if category != "O"
        else ""
    )
    buyer_name = _xml_escape(inv.buyer.get("name") or inv.buyer["email"])
    buyer_address_fragment = ""
    buyer_country = (inv.buyer.get("country") or "").strip()
    # A bare ISO 3166-1 alpha-2 code ("FR", "jp") is accepted directly; names go through the map.
    if len(buyer_country) == 2 and buyer_country.isalpha():
        country_id = buyer_country.upper()
    else:
        country_id = _COUNTRY_NAME_TO_ISO2.get(buyer_country.lower())
    if buyer_country and not country_id:
        logger.warning(
            f"Invoice {inv.number}: buyer country {buyer_country!r} has no ISO2 mapping; CII address omitted"
        )
    # EN16931's PostalTradeAddress requires CountryID; buyer country is unvalidated free text,
    # so the whole block is only emitted when it maps to a known ISO 3166-1 alpha-2 code.
    if country_id and (inv.buyer.get("address_line1") or inv.buyer.get("postal_code") or inv.buyer.get("city")):
        line_two_xml = (
            f"\n          <ram:LineTwo>{_xml_escape(inv.buyer['address_line2'])}</ram:LineTwo>"
            if inv.buyer.get("address_line2")
            else ""
        )
        buyer_address_fragment = f"""
        <ram:PostalTradeAddress>
          <ram:PostcodeCode>{_xml_escape(inv.buyer.get("postal_code") or "")}</ram:PostcodeCode>
          <ram:LineOne>{_xml_escape(inv.buyer.get("address_line1") or "")}</ram:LineOne>{line_two_xml}
          <ram:CityName>{_xml_escape(inv.buyer.get("city") or "")}</ram:CityName>
          <ram:CountryID>{country_id}</ram:CountryID>
        </ram:PostalTradeAddress>"""
    seller = inv.seller
    issue = inv.issued_at.strftime("%Y%m%d")
    xml = f"""<?xml version='1.0' encoding='UTF-8'?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
  xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter>
      <ram:ID>urn:cen.eu:en16931:2017</ram:ID>
    </ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
  <rsm:ExchangedDocument>
    <ram:ID>{inv.number}</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime><udt:DateTimeString format="102">{issue}</udt:DateTimeString></ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>1</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>{_xml_escape(inv.line_label)}</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice><ram:ChargeAmount>{inv.net_amount}</ram:ChargeAmount></ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode="C62">1</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>{line_tax_fragment}
        <ram:SpecifiedTradeSettlementLineMonetarySummation>
          <ram:LineTotalAmount>{inv.net_amount}</ram:LineTotalAmount>
        </ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>{_xml_escape(seller["legal_name"])}</ram:Name>
        <ram:SpecifiedLegalOrganization>
          <ram:ID schemeID="0002">{seller["siren"]}</ram:ID>
        </ram:SpecifiedLegalOrganization>
        <ram:PostalTradeAddress>
          <ram:PostcodeCode>{_xml_escape(seller["postal_code"])}</ram:PostcodeCode>
          <ram:LineOne>{_xml_escape(seller["address_line1"])}</ram:LineOne>
          <ram:CityName>{_xml_escape(seller["city"])}</ram:CityName>
          <ram:CountryID>FR</ram:CountryID>
        </ram:PostalTradeAddress>
        {seller_vat_xml}
      </ram:SellerTradeParty>
      <ram:BuyerTradeParty><ram:Name>{buyer_name}</ram:Name>{buyer_address_fragment}
      </ram:BuyerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery>
      <ram:ActualDeliverySupplyChainEvent>
        <ram:OccurrenceDateTime><udt:DateTimeString format="102">{inv.payment_date.strftime("%Y%m%d")}</udt:DateTimeString></ram:OccurrenceDateTime>
      </ram:ActualDeliverySupplyChainEvent>
    </ram:ApplicableHeaderTradeDelivery>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>{inv.currency}</ram:InvoiceCurrencyCode>{tax_fragment}
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>{inv.net_amount}</ram:LineTotalAmount>
        <ram:TaxBasisTotalAmount>{inv.net_amount}</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="{inv.currency}">{inv.vat_amount}</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>{inv.gross_amount}</ram:GrandTotalAmount>
        <ram:TotalPrepaidAmount>{inv.gross_amount}</ram:TotalPrepaidAmount>
        <ram:DuePayableAmount>0.00</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""
    return xml.encode("utf-8")


def _render_html(invoice: Invoice) -> str:
    logo = Markup("".join(f'<path d="{d}" fill="#111111"/>' for d in LOGO_PATHS))
    return _env.get_template("invoice.html.j2").render(inv=invoice, logo_paths=logo)


def render_invoice_pdf(invoice: Invoice) -> bytes:
    pdf = HTML(string=_render_html(invoice)).write_pdf(pdf_variant="pdf/a-3b")
    return generate_from_binary(pdf, build_cii_xml(invoice), flavor="factur-x", level="en16931")


async def get_or_render_pdf(db: AsyncSession, invoice: Invoice) -> bytes:
    if invoice.pdf is not None:
        return invoice.pdf
    # Concurrent first downloads serialize here; the second sees the winner's bytes on re-read.
    locked = (await db.execute(select(Invoice).where(Invoice.id == invoice.id).with_for_update())).scalar_one()
    if locked.pdf is not None:
        # Same session, same identity-map instance as ``invoice`` — no stale-attribute hazard.
        return locked.pdf
    rendered = await anyio.to_thread.run_sync(render_invoice_pdf, invoice)
    invoice.pdf = rendered
    invoice.template_version = TEMPLATE_VERSION
    await db.flush()
    return rendered
