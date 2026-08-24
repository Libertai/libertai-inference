"""Lifecycle email service: suppression rules, send log, unsubscribe flow."""

from sqlalchemy import select

import src.services.lifecycle_email as lifecycle
from src.config import config
from src.models.base import AsyncSessionLocal
from src.models.lifecycle_email_send import LifecycleEmailSend
from src.models.user import User
from src.services.users import get_or_create_user_by_email


async def _make_user(email: str | None):
    async with AsyncSessionLocal() as db:
        if email is None:
            user = User()
            db.add(user)
            await db.flush()
        else:
            user, _ = await get_or_create_user_by_email(db, email)
        await db.commit()
        return user.id


async def _send(user_id, email_type="welcome", **kwargs) -> bool:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        sent = await lifecycle.send_lifecycle_email(db, user, email_type, "paid_welcome", {"tier": "go"}, **kwargs)
        await db.commit()
        return sent


async def _sends(user_id) -> list[LifecycleEmailSend]:
    async with AsyncSessionLocal() as db:
        return list(
            (await db.execute(select(LifecycleEmailSend).where(LifecycleEmailSend.user_id == user_id))).scalars().all()
        )


async def test_send_records_and_dedups():
    user_id = await _make_user("lifecycle-dedup@example.com")
    assert await _send(user_id) is True
    assert await _send(user_id) is False  # same type, once=True
    rows = await _sends(user_id)
    assert len(rows) == 1 and rows[0].email_type == "welcome" and rows[0].transactional is False


async def test_no_email_never_sends():
    user_id = await _make_user(None)
    assert await _send(user_id) is False
    assert await _sends(user_id) == []


async def test_opt_out_blocks_lifecycle_but_not_transactional():
    user_id = await _make_user("lifecycle-optout@example.com")
    async with AsyncSessionLocal() as db:
        (await db.get(User, user_id)).lifecycle_emails_opt_out = True
        await db.commit()
    assert await _send(user_id, "checkin") is False
    assert await _send(user_id, "payment_failed", transactional=True) is True


async def test_weekly_cap_blocks_third_lifecycle_email():
    user_id = await _make_user("lifecycle-cap@example.com")
    assert await _send(user_id, "welcome") is True
    assert await _send(user_id, "checkin") is True
    assert await _send(user_id, "survey") is False  # cap = 2 per 7 days
    # Transactional is exempt from the cap.
    assert await _send(user_id, "payment_failed", transactional=True) is True


async def test_once_false_allows_repeat_sends():
    user_id = await _make_user("lifecycle-repeat@example.com")
    assert await _send(user_id, "usage_warning", once=False) is True
    assert await _send(user_id, "usage_warning", once=False) is True
    assert len(await _sends(user_id)) == 2


async def test_lifecycle_email_carries_unsubscribe_transactional_does_not(monkeypatch):
    captured = []

    async def fake_send(to, subject, html, headers=None, sender=None):
        captured.append((to, subject, html, headers, sender))
        return True

    monkeypatch.setattr(lifecycle, "send_email", fake_send)
    user_id = await _make_user("lifecycle-headers@example.com")

    assert await _send(user_id, "welcome") is True
    _, _, html, headers, _ = captured[0]
    assert "/emails/unsubscribe?token=" in html
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert lifecycle.parse_unsubscribe_token(headers["List-Unsubscribe"].split("token=")[1].rstrip(">")) == str(
        user_id
    )

    assert await _send(user_id, "payment_failed", transactional=True) is True
    _, _, html, headers, _ = captured[1]
    assert "unsubscribe" not in html.lower() and headers is None


async def test_sent_from_the_monitored_mailbox(monkeypatch):
    """Templates ask the user to reply, so the sender must not be the noreply address."""
    captured = []

    async def fake_send(to, subject, html, headers=None, sender=None):
        captured.append(sender)
        return True

    monkeypatch.setattr(lifecycle, "send_email", fake_send)
    user_id = await _make_user("lifecycle-sender@example.com")

    assert await _send(user_id, "welcome") is True
    assert captured == [config.SMTP_FROM_LIFECYCLE]
    assert "noreply" not in config.SMTP_FROM_LIFECYCLE


async def test_failed_transport_not_recorded(monkeypatch):
    async def fake_send(to, subject, html, headers=None, sender=None):
        return False

    monkeypatch.setattr(lifecycle, "send_email", fake_send)
    user_id = await _make_user("lifecycle-fail@example.com")
    assert await _send(user_id) is False
    assert await _sends(user_id) == []


async def test_unsubscribe_endpoints(async_client):
    user_id = await _make_user("lifecycle-unsub@example.com")
    token = lifecycle.build_unsubscribe_token(user_id)

    resp = await async_client.get("/emails/unsubscribe", params={"token": token})
    assert resp.status_code == 200 and "unsubscribed" in resp.text.lower()
    async with AsyncSessionLocal() as db:
        assert (await db.get(User, user_id)).lifecycle_emails_opt_out is True

    # One-click POST (RFC 8058) works too, and is idempotent.
    resp = await async_client.post("/emails/unsubscribe", params={"token": token})
    assert resp.status_code == 204

    assert (await async_client.get("/emails/unsubscribe", params={"token": "garbage"})).status_code == 400


async def test_unsubscribe_survives_a_deleted_account(async_client):
    """The link was valid; there is simply nothing left to opt out."""
    user_id = await _make_user("lifecycle-deleted@example.com")
    token = lifecycle.build_unsubscribe_token(user_id)
    async with AsyncSessionLocal() as db:
        await db.delete(await db.get(User, user_id))
        await db.commit()

    assert (await async_client.get("/emails/unsubscribe", params={"token": token})).status_code == 200
    assert (await async_client.post("/emails/unsubscribe", params={"token": token})).status_code == 204


async def test_email_logo_served(async_client):
    resp = await async_client.get("/emails/logo.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
