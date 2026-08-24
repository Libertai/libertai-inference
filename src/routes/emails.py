"""Email preference endpoints. Unauthenticated: the signed token is the credential,
so unsubscribe works from any mail client without a session."""

import uuid

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse

from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.lifecycle_email import parse_unsubscribe_token

router = APIRouter(prefix="/emails", tags=["emails"])

_CONFIRMATION_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Unsubscribed — LibertAI</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center;color:#111214">
<h1 style="font-size:24px">You're unsubscribed</h1>
<p style="color:#50545b">You will no longer receive product and onboarding emails from LibertAI.
Sign-in and billing emails are unaffected.</p>
</body></html>"""


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
    return _CONFIRMATION_HTML


@router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe_post(token: str) -> None:
    """RFC 8058 one-click unsubscribe, called by the mail provider rather than the user."""
    await _unsubscribe(token)
