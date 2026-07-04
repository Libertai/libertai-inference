"""Minimal async SMTP sender for transactional mails (invites, seat-lapse notices).

Mirrors the magic-link mailer's convention: with no SMTP_HOST configured (dev),
log the mail instead of sending.
"""

import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.config import config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def _send_smtp(to: list[str], subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
        if config.SMTP_USE_TLS:
            server.starttls()
        if config.SMTP_USER and config.SMTP_PASSWORD:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.sendmail(config.SMTP_FROM, to, msg.as_string())


async def send_email(to: list[str], subject: str, html: str) -> None:
    if not to:
        return
    if not config.SMTP_HOST:
        logger.warning(f"[email mock] to={to} subject={subject!r}")
        return
    try:
        await asyncio.to_thread(_send_smtp, to, subject, html)
        logger.info(f"Email {subject!r} sent to {len(to)} recipient(s)")
    except Exception as e:
        # Transactional mail must never break the calling flow; log for ops.
        logger.error(f"SMTP send failed for {to}: {e}")
