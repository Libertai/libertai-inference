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

_reader: geoip2.database.Reader | None = None


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
    except Exception:
        logger.warning("GeoIP lookup failed; defaulting to USD", exc_info=True)
        return "USD"
    return "EUR" if country in EU_COUNTRIES else "USD"
