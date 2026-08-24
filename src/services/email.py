"""Shared email transport. With no SMTP host configured (dev), sends are logged and reported OK."""

import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.config import config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


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
    to surface the error to. `sender` sets the From and envelope sender, so it must be an address
    the SMTP account owns.
    """
    if not config.SMTP_HOST:
        logger.warning(f"[email mock] to={to} subject={subject!r}")
        return True
    try:
        await asyncio.to_thread(_send_smtp, to, subject, html, headers, sender or config.SMTP_FROM)
        logger.info(f"Email sent to {to} ({subject!r})")
        return True
    except Exception as e:
        logger.error(f"SMTP send failed for {to}: {e}")
        return False
