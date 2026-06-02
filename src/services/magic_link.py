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


async def send_magic_link_email(email: str, token: str, code: str) -> None:
    """Send the magic-link email via Resend. With no API key configured (dev/mock), just log it."""
    link = f"{config.FRONTEND_URL}/auth/verify?token={token}"
    if not config.RESEND_API_KEY:
        logger.warning(f"[magic-link mock] to={email} code={code} link={link}")
        return

    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json={
                "from": "LibertAI <noreply@libertai.io>",
                "to": [email],
                "subject": "Your LibertAI sign-in link",
                "html": f'Sign in with code <b>{code}</b> or <a href="{link}">click here</a>.',
            },
        )
