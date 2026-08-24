"""Lifecycle email sending: suppression rules, send log, unsubscribe tokens.

Callers own the transaction (a send flushes a LifecycleEmailSend row, never commits) and the
concurrency: cron senders must hold a pg advisory lock, since the app runs multiple replicas.
"""

from datetime import datetime, timedelta

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import config
from src.models.lifecycle_email_send import LifecycleEmailSend
from src.models.user import User
from src.services.email import send_email
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Transactional emails are exempt from the cap and uncounted.
_WEEKLY_CAP = 2
_CAP_WINDOW = timedelta(days=7)


# Tokens never expire: an unsubscribe link must keep working forever.
def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(config.MAGIC_LINK_SECRET, salt="lifecycle-unsubscribe")


def build_unsubscribe_token(user_id) -> str:
    return _serializer().dumps(str(user_id))


def parse_unsubscribe_token(token: str) -> str | None:
    try:
        return _serializer().loads(token)
    except BadSignature:
        return None


def build_unsubscribe_url(user_id) -> str:
    return f"{config.API_URL}/emails/unsubscribe?token={build_unsubscribe_token(user_id)}"


def _footer(unsubscribe_url: str) -> str:
    return (
        '<p style="margin-top:32px;font-size:12px;color:#8a8f97">'
        "You received this email because you have a LibertAI account. "
        f'<a href="{unsubscribe_url}" style="color:#8a8f97">Unsubscribe</a> '
        "from these emails at any time (sign-in and billing emails are unaffected)."
        "</p>"
    )


async def send_lifecycle_email(
    db: AsyncSession,
    user: User,
    email_type: str,
    subject: str,
    html: str,
    *,
    transactional: bool = False,
    once: bool = True,
) -> bool:
    """Send one email, applying suppression rules. True iff sent.

    `once` never resends an email_type to a user; repeatable types pace themselves.
    """
    if not user.email:
        return False

    if not transactional and user.lifecycle_emails_opt_out:
        return False

    if once:
        already = await db.scalar(
            select(func.count())
            .select_from(LifecycleEmailSend)
            .where(LifecycleEmailSend.user_id == user.id, LifecycleEmailSend.email_type == email_type)
        )
        if already:
            return False

    if not transactional:
        recent = await db.scalar(
            select(func.count())
            .select_from(LifecycleEmailSend)
            .where(
                LifecycleEmailSend.user_id == user.id,
                LifecycleEmailSend.transactional.is_(False),
                LifecycleEmailSend.sent_at > datetime.now() - _CAP_WINDOW,
            )
        )
        if recent is not None and recent >= _WEEKLY_CAP:
            logger.info(f"Lifecycle email {email_type!r} to user {user.id} skipped: weekly cap reached")
            return False

    headers: dict[str, str] | None = None
    if not transactional:
        unsubscribe_url = build_unsubscribe_url(user.id)
        html = html + _footer(unsubscribe_url)
        # RFC 8058 one-click unsubscribe; Gmail/Yahoo require these for non-transactional mail.
        headers = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }

    if not await send_email(user.email, subject, html, headers, sender=config.SMTP_FROM_LIFECYCLE):
        return False

    db.add(LifecycleEmailSend(user_id=user.id, email_type=email_type, transactional=transactional))
    await db.flush()
    return True
