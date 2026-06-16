import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.config import config
from src.services.chat_rate_limit import client_identity, enforce_free_chat_rate_limit, reset_free_chat_rate_limits

pytestmark = pytest.mark.asyncio


def _request(ip: str = "127.0.0.1", forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    return Request({"type": "http", "method": "POST", "path": "/chat/completions", "headers": headers, "client": (ip, 1234)})


async def test_free_chat_rate_limit_uses_forwarded_ip():
    request = _request(forwarded_for="203.0.113.10, 10.0.0.1")
    assert client_identity(request) == "203.0.113.10"


async def test_free_chat_rate_limit_rejects_after_minute_limit(monkeypatch):
    await reset_free_chat_rate_limits()
    monkeypatch.setattr(config, "FREE_CHAT_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(config, "FREE_CHAT_RATE_LIMIT_PER_DAY", 100)
    request = _request()

    await enforce_free_chat_rate_limit(request, now=1000)

    with pytest.raises(HTTPException) as exc:
        await enforce_free_chat_rate_limit(request, now=1001)

    assert exc.value.status_code == 429
    assert exc.value.headers == {"Retry-After": "59"}
    assert exc.value.detail["scope"] == "free_chat_requests_per_minute"


async def test_free_chat_rate_limit_resets_window(monkeypatch):
    await reset_free_chat_rate_limits()
    monkeypatch.setattr(config, "FREE_CHAT_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(config, "FREE_CHAT_RATE_LIMIT_PER_DAY", 100)
    request = _request()

    await enforce_free_chat_rate_limit(request, now=1000)
    await enforce_free_chat_rate_limit(request, now=1060)


async def test_free_chat_rate_limit_can_be_disabled(monkeypatch):
    await reset_free_chat_rate_limits()
    monkeypatch.setattr(config, "FREE_CHAT_RATE_LIMIT_PER_MINUTE", 0)
    monkeypatch.setattr(config, "FREE_CHAT_RATE_LIMIT_PER_DAY", 0)
    request = _request()

    await enforce_free_chat_rate_limit(request, now=1000)
    await enforce_free_chat_rate_limit(request, now=1001)
