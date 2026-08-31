"""Shared email transport (Resend). With no API key configured (dev), sends are logged and reported OK."""

import httpx

from src.config import config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Shared client so batched sends (the lifecycle cron) reuse pooled keep-alive connections.

    Built lazily on first send: a module-level client would bind its pool to whatever event loop
    imported it.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15)
    return _client


async def close_client() -> None:
    """Close the shared client to release connections. Called from the app lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def send_email(
    to: str, subject: str, html: str, headers: dict[str, str] | None = None, sender: str | None = None
) -> bool:
    """Send an HTML email. True on success (or in dev mock mode), False on transport failure.

    Never raises: callers must not leak whether an address exists, and cron callers have no one
    to surface the error to. `sender` sets the From, so its domain must be verified in Resend.
    """
    if not config.RESEND_API_KEY:
        logger.warning(f"[email mock] to={to} subject={subject!r}")
        return True

    payload: dict[str, object] = {
        "from": sender or config.EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if headers:
        payload["headers"] = headers

    try:
        response = await _get_client().post(
            _RESEND_ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Resend puts the reason (unverified domain, invalid recipient) in the body, not the status.
        logger.error(f"Resend send failed for {to}: {e.response.status_code} {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"Resend send failed for {to}: {e}")
        return False

    logger.info(f"Email sent to {to} ({subject!r})")
    return True
