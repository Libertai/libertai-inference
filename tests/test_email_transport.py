"""Login mail goes over the Proton SMTP account, falling back to Resend when SMTP is unset."""

import pytest

from src.config import config
from src.services import email

pytestmark = pytest.mark.asyncio


async def test_login_email_uses_smtp_when_configured(monkeypatch):
    sent = {}

    def fake_smtp(to, subject, html, headers, sender):
        sent.update(to=to, subject=subject, html=html, headers=headers, sender=sender)

    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_FROM", "LibertAI <hello@libertai.io>")
    monkeypatch.setattr(email, "_send_smtp", fake_smtp)

    assert await email.send_login_email("a@example.com", "subject", "<p>hi</p>") is True
    assert sent["sender"] == "LibertAI <hello@libertai.io>"
    assert sent["to"] == "a@example.com"


async def test_login_email_falls_back_to_resend(monkeypatch):
    sent = {}

    async def fake_send(to, subject, html, headers=None, sender=None):
        sent.update(to=to, subject=subject)
        return True

    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(email, "send_email", fake_send)

    assert await email.send_login_email("a@example.com", "subject", "<p>hi</p>") is True
    assert sent["to"] == "a@example.com"
