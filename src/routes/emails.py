"""Email preference endpoints and lifecycle email cron jobs.

Endpoints are unauthenticated: the signed token is the credential, so unsubscribe works
from any mail client without a session."""

import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select

from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.entitlement import current_month_bounds, month_overflow_by_users
from src.services.lifecycle_email import parse_unsubscribe_token, render_page, send_lifecycle_email
from src.utils.cron import scheduler
from src.utils.logger import setup_logger
from src.utils.pg_locks import LIFECYCLE_EMAILS_LOCK_ID, single_runner

logger = setup_logger(__name__)

router = APIRouter(prefix="/emails", tags=["emails"])


async def check_extra_usage_caps() -> int:
    """Warn users approaching their monthly extra-usage credit cap, once per threshold.

    The cap and its overflow counter reset monthly, so the dedup window is "since month start".
    """
    now = datetime.now()
    month_start, _ = current_month_bounds(now)
    sent = 0
    async with AsyncSessionLocal() as db:
        users = (
            (
                await db.execute(
                    select(User).where(
                        User.monthly_extra_credit_cap.is_not(None),
                        User.monthly_extra_credit_cap > 0,
                        User.email.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not users:
            return 0
        overflow = await month_overflow_by_users(db, {u.id for u in users}, now)
        for user in users:
            cap = user.monthly_extra_credit_cap
            assert cap is not None  # filtered above; for the type checker
            pct = overflow.get(user.id, 0.0) / cap * 100
            if pct >= 100:
                # Their keys are already refused with ``extra_credit_cap``, so a warning that
                # usage "will pause" is false. There is no cap-reached email to send instead.
                continue
            threshold = 90 if pct >= 90 else 75 if pct >= 75 else None
            if threshold is None:
                continue
            try:
                if await send_lifecycle_email(
                    db,
                    user,
                    f"extra_usage_cap_{threshold}",
                    "extra_usage_cap",
                    {"pct": int(pct), "cap": f"{cap:g}"},
                    transactional=True,
                    once=False,
                    resend_after=now - month_start,
                ):
                    # Commit per send: the mail is already gone, so its log row has to survive a
                    # later failure in the sweep. Rolling it back would re-send on the next run.
                    await db.commit()
                    sent += 1
            except Exception:
                logger.error(f"Extra-usage-cap warning failed for user {user.id}", exc_info=True)
                await db.rollback()
    return sent


# 10 min, not hourly: an API burst can cross both thresholds well within an hour,
# and a warning that arrives after the cap is hit is useless.
@scheduler.scheduled_job("interval", minutes=10)
@single_runner(LIFECYCLE_EMAILS_LOCK_ID, skip_result=0)
async def send_extra_usage_cap_warnings() -> int:
    return await check_extra_usage_caps()


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "emails"


@router.get("/logo.png", include_in_schema=False)
async def email_logo() -> FileResponse:
    # Referenced by URL from the templates: mail clients don't render data-URI images.
    return FileResponse(_STATIC_DIR / "logo.png", headers={"Cache-Control": "public, max-age=604800"})


async def _unsubscribe(token: str) -> None:
    user_id = parse_unsubscribe_token(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid unsubscribe link")
    async with AsyncSessionLocal() as db:
        user = await db.get(User, uuid.UUID(user_id))
        # Deleted account: nothing to update, but the link itself was valid — report success.
        if user is not None:
            user.lifecycle_emails_opt_out = True
        await db.commit()


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_get(token: str) -> str:
    await _unsubscribe(token)
    return render_page("unsubscribe_page")


@router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe_post(token: str) -> None:
    """RFC 8058 one-click unsubscribe, called by the mail provider rather than the user."""
    await _unsubscribe(token)
