"""Resolve which frontend a flow should return the user to (chat vs console).

Shared by OAuth, magic links and payment checkouts: the caller-supplied origin is honoured
only when it's an allowlisted frontend, never an attacker-supplied URL."""

from src.config import config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def resolve_frontend_base(redirect_base: str | None) -> str:
    """Honour the caller's origin only if it's an allowed frontend (chat vs console);
    else fall back to the default FRONTEND_URL."""
    if redirect_base:
        # Scheme/host are case-insensitive (RFC 3986); allowlist entries are lowercase already.
        candidate = redirect_base.rstrip("/").lower()
        if candidate in {url.rstrip("/").lower() for url in config.ALLOWED_FRONTEND_URLS}:
            return candidate
        logger.warning(f"Ignoring disallowed redirect_base: {redirect_base}")
    base = config.FRONTEND_URL.rstrip("/")
    if not base:
        # An empty base would yield relative URLs (e.g. a "/payment/callback" handed to Revolut)
        # that only fail later with an opaque error — fail loudly here instead.
        raise ValueError("FRONTEND_URL is not configured")
    return base
