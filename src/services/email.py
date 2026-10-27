"""Email transports: Proton SMTP for sign-in links, Resend for lifecycle mail.

With neither configured (dev), sends are logged and reported OK.
"""

import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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


def _send_smtp(to: str, subject: str, html: str, headers: dict[str, str] | None, sender: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
        if config.SMTP_USE_TLS:
            server.starttls()
        if config.SMTP_USER and config.SMTP_PASSWORD:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.sendmail(sender, [to], msg.as_string())


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


async def send_login_email(to: str, subject: str, html: str) -> bool:
    """Send a sign-in email through the Proton SMTP account.

    Falls back to Resend when SMTP is not configured, so deployments mid-migration still deliver.
    True on success (or in dev mock mode), False on transport failure.
    """
    if not config.SMTP_HOST:
        return await send_email(to, subject, html)

    try:
        await asyncio.to_thread(_send_smtp, to, subject, html, None, config.SMTP_FROM)
        logger.info(f"Login email sent to {to} ({subject!r})")
        return True
    except Exception as e:
        logger.error(f"SMTP send failed for {to}: {e}")
        return False
