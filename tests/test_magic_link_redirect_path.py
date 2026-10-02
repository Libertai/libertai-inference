"""The magic link carries an optional same-origin landing path (`next`), e.g. back to /claim?e=<code>,
so signing in from the email lands where the user started even in another tab or browser."""

import pytest

from src.config import config
from src.services import magic_link

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/claim?e=cg83tswx", "/claim?e=cg83tswx"),
        ("/dashboard", "/dashboard"),
        (None, None),
        ("", None),
        ("claim", None),
        ("//evil.example/x", None),
        ("/\\evil.example", None),
        ("https://evil.example/", None),
        ("/claim\r\nSet-Cookie: x", None),
        ("/" + "a" * 600, None),
    ],
)
def test_safe_redirect_path(path, expected):
    assert magic_link.safe_redirect_path(path) == expected


async def _sent_link(monkeypatch, redirect_path):
    sent = {}

    async def fake_send(to, subject, html):
        sent["html"] = html

    monkeypatch.setattr(config, "RESEND_API_KEY", "test")
    monkeypatch.setattr(config, "FRONTEND_URL", "https://console.libertai.io")
    monkeypatch.setattr(magic_link, "send_email", fake_send)
    await magic_link.send_magic_link_email("a@example.com", "tok", "123456", None, redirect_path)
    return sent["html"]


async def test_link_carries_next(monkeypatch):
    html = await _sent_link(monkeypatch, "/claim?e=cg83tswx")
    assert "/auth/verify?token=tok&amp;next=%2Fclaim%3Fe%3Dcg83tswx" in html or (
        "/auth/verify?token=tok&next=%2Fclaim%3Fe%3Dcg83tswx" in html
    )


async def test_unsafe_path_is_dropped(monkeypatch):
    html = await _sent_link(monkeypatch, "//evil.example")
    assert "next=" not in html
