"""Shared frontend-base resolution (OAuth, magic links, payment checkouts): only ever return the
user to an allowlisted frontend (chat vs console), never an attacker-supplied URL, and fall back
to FRONTEND_URL otherwise."""

import pytest

from src.config import config
from src.utils.frontend import resolve_frontend_base

# Distinct from every allowlisted URL so fallback assertions can't pass by accident
# (i.e. they'd still fail if validation were removed and the input echoed back).
_FALLBACK = "https://fallback.example"


def _patch(monkeypatch, allowed: list[str], fallback: str = _FALLBACK) -> None:
    monkeypatch.setattr(config, "ALLOWED_FRONTEND_URLS", allowed)
    monkeypatch.setattr(config, "FRONTEND_URL", fallback)


def test_allowed_origin_is_honoured(monkeypatch):
    _patch(monkeypatch, ["https://chat.libertai.io", "https://console.libertai.io"])

    assert resolve_frontend_base("https://chat.libertai.io") == "https://chat.libertai.io"
    # Trailing slash is normalised away.
    assert resolve_frontend_base("https://chat.libertai.io/") == "https://chat.libertai.io"
    # Scheme/host are case-insensitive (RFC 3986): uppercase variants are still allowed.
    assert resolve_frontend_base("HTTPS://chat.libertai.io") == "https://chat.libertai.io"


def test_disallowed_origin_falls_back_to_default(monkeypatch):
    _patch(monkeypatch, ["https://chat.libertai.io"])

    # An attacker-supplied origin must NOT be used — fall back to the default frontend.
    result = resolve_frontend_base("https://evil.example.com")
    assert result == _FALLBACK
    assert result != "https://evil.example.com"

    # A lookalike host that merely starts with an allowed origin is still disallowed.
    lookalike = resolve_frontend_base("https://chat.libertai.io.evil.com")
    assert lookalike == _FALLBACK
    assert lookalike != "https://chat.libertai.io.evil.com"


def test_missing_origin_falls_back_to_default(monkeypatch):
    _patch(monkeypatch, ["https://chat.libertai.io"])

    assert resolve_frontend_base(None) == _FALLBACK


def test_empty_fallback_raises(monkeypatch):
    _patch(monkeypatch, ["https://chat.libertai.io"], fallback="")

    # An empty FRONTEND_URL would produce relative redirect URLs that fail opaquely downstream.
    with pytest.raises(ValueError, match="FRONTEND_URL"):
        resolve_frontend_base(None)
    with pytest.raises(ValueError, match="FRONTEND_URL"):
        resolve_frontend_base("https://evil.example.com")

    # An allowed origin never hits the fallback, so it still resolves fine.
    assert resolve_frontend_base("https://chat.libertai.io") == "https://chat.libertai.io"
