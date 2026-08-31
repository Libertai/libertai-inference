"""Lifecycle email sending: rendering, suppression rules, send log, unsubscribe.

Callers own the transaction (a send flushes a LifecycleEmailSend row, never commits) and the
concurrency: cron senders must hold a pg advisory lock, since the app runs multiple replicas.
"""

from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import config
from src.models.lifecycle_email_send import LifecycleEmailSend
from src.models.user import User
from src.services.email import send_email
from src.utils.frontend import resolve_frontend_base
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "emails"

# Transactional emails are exempt from the cap and uncounted.
_WEEKLY_CAP = 2
_CAP_WINDOW = timedelta(days=7)


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=True)


def _frontend() -> str:
    try:
        return resolve_frontend_base(None)
    except ValueError:  # FRONTEND_URL unset (dev): emit a valid link rather than crash a send
        return "https://chat.libertai.io"


def render_email(
    template: str, user: User, ctx: dict | None = None, unsubscribe_url: str | None = None
) -> tuple[str, str]:
    tmpl = _env().get_template(f"{template}.html.j2")
    context = {
        "display_name": user.display_name,
        "frontend": _frontend(),
        "api_url": config.API_URL,
        "unsubscribe_url": unsubscribe_url,
        **(ctx or {}),
    }
    subject = getattr(tmpl.make_module(context), "subject", None)
    if not subject:
        raise ValueError(f"Email template {template} does not set a subject")
    return str(subject), tmpl.render(context)


def render_page(template: str) -> str:
    return _env().get_template(f"{template}.html.j2").render()


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


async def send_lifecycle_email(
    db: AsyncSession,
    user: User,
    email_type: str,
    template: str,
    ctx: dict | None = None,
    *,
    transactional: bool = False,
    once: bool = True,
    resend_after: timedelta | None = None,
) -> bool:
    """Send one email, applying suppression rules. True iff sent.

    `once` never resends an email_type to a user, and short-circuits `resend_after`, which sets
    the minimum gap between repeats of a type sent with `once=False`.
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

    if resend_after is not None:
        last = await db.scalar(
            select(func.max(LifecycleEmailSend.sent_at)).where(
                LifecycleEmailSend.user_id == user.id, LifecycleEmailSend.email_type == email_type
            )
        )
        if last is not None and last > datetime.now() - resend_after:
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
    unsubscribe_url: str | None = None
    if not transactional:
        unsubscribe_url = build_unsubscribe_url(user.id)
        # RFC 8058 one-click unsubscribe; Gmail/Yahoo require these for non-transactional mail.
        headers = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }

    subject, html = render_email(template, user, ctx, unsubscribe_url=unsubscribe_url)
    if not await send_email(user.email, subject, html, headers, sender=config.EMAIL_FROM_LIFECYCLE):
        return False

    db.add(LifecycleEmailSend(user_id=user.id, email_type=email_type, transactional=transactional))
    await db.flush()
    return True
