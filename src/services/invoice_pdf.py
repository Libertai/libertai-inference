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
from src.services.invoice import SERIES_LCLW
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

TEMPLATE_VERSION = 2

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

# Claw mark, path data from the LiberClaw repo (sites/landing/src/components/ClawLogo.astro).
CLAW_LOGO_PATH: str = (
    "m 133.21538,231.01417 c -3.85253,-0.36355 -5.45102,-0.57997 -7.84115,-1.06158 -2.74646,-0.55341 "
    "-3.58746,-0.76052 -6.32907,-1.55862 -1.59693,-0.46487 -4.69751,-1.573 -6.14374,-2.19573 -0.54501,"
    "-0.23467 -1.43684,-0.61785 -1.98185,-0.85151 -22.038171,-9.44819 -47.146911,-33.36567 -60.492881,"
    "-57.62285 -0.32984,-0.59951 -0.89333,-1.62512 -1.25219,-2.27913 -1.84493,-3.3623 -6.06796,-12.51793 "
    "-5.86672,-12.71917 0.0228,-0.0228 2.40233,0.0505 5.28795,0.16289 14.24567,0.55462 31.40666,-1.04283 "
    "52.383111,-4.87615 5.06306,-0.92524 5.67573,-1.00618 5.68538,-0.75103 0.003,0.0817 0.36388,0.54996 "
    "0.80175,1.04047 2.34831,2.63063 5.05475,7.02697 8.35736,13.57567 0.87954,1.74402 1.97559,4.0182 "
    "2.43566,5.05371 4.37623,9.84986 4.97974,11.16678 6.67543,14.5666 1.25894,2.52414 3.32386,6.21737 "
    "3.76287,6.7301 0.0972,0.11351 0.47954,0.67463 0.84968,1.24694 8.70818,13.46461 23.60707,16.92751 "
    "44.08434,10.24639 0.80992,-0.26425 1.50008,-0.45296 1.53368,-0.41936 0.10716,0.10716 -2.38574,"
    "5.72614 -3.1798,7.16723 -0.42042,0.76301 -0.94415,1.74403 -1.16384,2.18003 -0.21968,0.43601 "
    "-0.4575,0.83734 -0.52848,0.89184 -0.071,0.0545 -0.50663,0.72337 -0.9681,1.48638 -0.46147,0.76302 "
    "-0.94904,1.56417 -1.08348,1.78036 -3.21843,5.17507 -9.94086,11.30373 -15.50469,14.13524 -5.58864,"
    "2.84412 -13.97999,4.59421 -19.52122,4.07128 z m 82.0479,-18.94132 c 0,-0.24805 -0.0883,-1.47661 "
    "-0.19534,-2.73013 -0.10703,-1.25352 -0.2898,-3.39392 -0.4061,-4.75644 -1.3617,-15.95221 -4.30174,"
    "-29.63112 -8.57253,-39.88472 -0.48805,-1.17177 -0.83071,-2.13185 -0.76144,-2.13352 0.0693,-0.002 "
    "0.0334,-0.0686 -0.0798,-0.14864 -0.11313,-0.0801 -0.72461,-1.17121 -1.35886,-2.42473 -10.02677,"
    "-19.81693 -27.76901,-26.76571 -57.69275,-22.5955 -8.05722,1.12287 -14.33711,2.22475 -28.24135,"
    "4.95526 -25.706301,5.04821 -36.016791,6.62758 -50.735351,7.77166 -4.45241,0.34608 -22.06785,"
    "0.34518 -26.06132,-0.001 -7.88362,-0.68404 -15.77474,-1.94282 -22.69218,-3.61979 l -2.47731,"
    "-0.60057 -1.47466,-3.21091 C -0.5438511,109.90633 -2.3201111,83.433352 9.2191289,63.776262 "
    "19.822459,45.713512 42.453789,33.891302 70.291629,31.873072 c 3.92406,-0.28449 8.73846,-0.64716 "
    "10.69866,-0.80593 21.119781,-1.71062 53.099751,3.37936 78.286381,12.46018 2.33505,0.84188 "
    "7.55954,2.8657 8.47843,3.2843 0.47751,0.21753 1.35152,0.58506 1.94225,0.81673 0.82926,0.32521 "
    "7.37561,3.62363 9.10054,4.58535 0.1635,0.0912 1.36747,0.66486 2.6755,1.27488 2.3306,1.08693 "
    "3.04084,1.41453 6.04464,2.78807 4.29876,1.96569 13.7049,7.8437 19.62031,12.26097 2.44534,"
    "1.82604 4.62425,3.61564 11.59382,9.52235 5.87291,4.9773 15.70512,16.50514 20.21486,23.701078 "
    "1.49727,2.38909 4.06279,6.66381 4.06279,6.76947 0,0.0645 0.27919,0.58933 0.6204,1.16638 "
    "7.77886,13.15531 12.51445,32.08404 11.39361,45.54172 -0.33352,4.00464 -1.02971,8.74973 "
    "-1.45388,9.90925 -0.0598,0.1635 -0.13479,0.49178 -0.16663,0.7295 -0.11725,0.87514 -1.08729,"
    "4.18243 -1.79146,6.10788 -0.39863,1.09002 -0.85049,2.3269 -1.00414,2.74863 -1.42348,3.9072 "
    "-5.95883,11.66588 -9.09893,15.56566 -3.83278,4.76003 -9.86929,10.80817 -13.57765,13.6038 "
    "-0.28166,0.21233 -1.53023,1.15392 -2.77459,2.09243 -2.2729,1.71422 -8.04607,5.58377 -9.24852,"
    "6.19895 -0.63449,0.32462 -0.64412,0.3228 -0.64477,-0.12148 z"
)

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
    if invoice.series == SERIES_LCLW:
        logo = Markup(f'<path d="{CLAW_LOGO_PATH}" fill="#111111"/>')
        viewbox, wordmark = "0 0 256 256", "LiberClaw"
    else:
        logo = Markup("".join(f'<path d="{d}" fill="#111111"/>' for d in LOGO_PATHS))
        viewbox, wordmark = "0 0 852 149", None
    return _env.get_template("invoice.html.j2").render(
        inv=invoice, logo_paths=logo, logo_viewbox=viewbox, wordmark=wordmark
    )


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
