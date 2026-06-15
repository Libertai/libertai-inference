import geoip2.database
from fastapi import Request

from src.config import config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# EU member states (VAT territory) — ISO 3166-1 alpha-2.
EU_COUNTRIES = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
}

# EU VAT rate applied to EUR sales. Pricing is gross/TTC: VAT is INCLUDED in the amount we
# charge (back-calculated for the invoice), never added on top. Single source of truth shared
# by /payments/region and the order line-item VAT breakdown.
EU_VAT_RATE = 0.20


def vat_rate_for_currency(currency: str) -> float:
    """VAT rate to apply for a charge currency. EUR -> EU VAT, else none."""
    return EU_VAT_RATE if currency == "EUR" else 0.0


_reader: geoip2.database.Reader | None = None
_warned_no_db = False  # log the missing-DB situation once, not per request


def _get_reader() -> geoip2.database.Reader:
    global _reader
    if _reader is None:
        _reader = geoip2.database.Reader(config.GEOIP_DB_PATH)  # raises if file missing
    return _reader


def client_ip(request: Request) -> str | None:
    """Real client IP: leftmost X-Forwarded-For entry (set by Traefik), else the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def resolve_currency(request: Request) -> str:
    """EU client IP -> EUR, else USD. Any failure (missing DB, bad IP) -> USD (safe default)."""
    ip = client_ip(request)
    if not ip:
        return "USD"
    try:
        country = _get_reader().country(ip).country.iso_code
    except FileNotFoundError:
        global _warned_no_db
        if not _warned_no_db:
            _warned_no_db = True
            logger.warning(
                f"GeoLite2 DB not found at {config.GEOIP_DB_PATH}; all users default to USD "
                "(further occurrences won't be logged)"
            )
        return "USD"
    except Exception:
        logger.warning("GeoIP lookup failed; defaulting to USD", exc_info=True)
        return "USD"
    return "EUR" if country in EU_COUNTRIES else "USD"
