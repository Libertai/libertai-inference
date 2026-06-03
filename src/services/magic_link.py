"""Passwordless email login: a signed link token + a 6-digit code, both hashed at rest."""

import hashlib
import secrets
from datetime import datetime, timedelta

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import config
from src.models.magic_link import MagicLink
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

_MAGIC_LINK_TTL_MINUTES = 15
_MAX_CODE_ATTEMPTS = 5


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.MAGIC_LINK_SECRET, salt="magic-link")


async def create_magic_link(db: AsyncSession, email: str) -> tuple[str, str]:
    """Create a magic link for an email, returning (token, code) to be emailed. Flushes, no commit."""
    email = email.strip().lower()
    token = _serializer().dumps(email)
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        MagicLink(
            email=email,
            token_hash=_hash(token),
            code_hash=_hash(code),
            expires_at=datetime.now() + timedelta(minutes=_MAGIC_LINK_TTL_MINUTES),
        )
    )
    await db.flush()
    return token, code


async def verify_magic_link(
    db: AsyncSession, token: str | None = None, email: str | None = None, code: str | None = None
) -> str | None:
    """Verify a magic link by token, or by (email, code). Returns the email on success, else None.

    Single-use: the matched link is marked used. Codes are limited to a few attempts.
    """
    now = datetime.now()

    if token:
        link = (await db.execute(select(MagicLink).where(MagicLink.token_hash == _hash(token)))).scalars().first()
        if link is None or link.used_at is not None or link.expires_at < now:
            return None
        link.used_at = now
        await db.flush()
        return link.email

    if email and code:
        email = email.strip().lower()
        link = (
            await db.execute(
                select(MagicLink)
                .where(MagicLink.email == email, MagicLink.used_at.is_(None))
                .order_by(MagicLink.created_at.desc())
            )
        ).scalars().first()
        if link is None or link.expires_at < now or link.code_hash is None:
            return None
        if link.attempts >= _MAX_CODE_ATTEMPTS:
            return None
        if not secrets.compare_digest(link.code_hash, _hash(code)):
            link.attempts += 1
            await db.flush()
            return None
        link.used_at = now
        await db.flush()
        return link.email

    return None


def _build_email_html(link: str, code: str) -> str:
    return (
        "<h2>Sign in to LibertAI</h2>"
        f'<p style="font-size: 24px; font-weight: bold; letter-spacing: 8px; text-align: center; '
        f'margin: 20px 0;">{code}</p>'
        '<p style="text-align: center; color: #666;">Enter this code in the app</p>'
        '<p style="text-align: center; color: #666;">&mdash; or &mdash;</p>'
        f'<p style="text-align: center;"><a href="{link}">Click here to sign in</a></p>'
        "<p>This link and code expire in 15 minutes.</p>"
        "<p>If you didn't request this, you can safely ignore this email.</p>"
    )


def _send_smtp(email: str, html: str) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your LibertAI sign-in link"
    msg["From"] = config.SMTP_FROM
    msg["To"] = email
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
        if config.SMTP_USE_TLS:
            server.starttls()
        if config.SMTP_USER and config.SMTP_PASSWORD:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.sendmail(config.SMTP_FROM, [email], msg.as_string())


async def send_magic_link_email(email: str, token: str, code: str) -> None:
    """Send the magic-link email via SMTP. With no SMTP host configured (dev), log it instead."""
    import asyncio

    link = f"{config.FRONTEND_URL}/auth/verify?token={token}"
    if not config.SMTP_HOST:
        logger.warning(f"[magic-link mock] to={email} code={code} link={link}")
        return

    try:
        await asyncio.to_thread(_send_smtp, email, _build_email_html(link, code))
        logger.info(f"Magic-link email sent to {email} via SMTP")
    except Exception as e:
        # Don't surface SMTP errors to the caller (avoids leaking whether an email exists);
        # the user can retry. The failure is logged for ops.
        logger.error(f"SMTP send failed for {email}: {e}")
