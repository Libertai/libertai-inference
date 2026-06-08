"""OAuth redirect_base resolution: the callback must only ever return the user to an allowlisted
frontend (chat vs console), never an attacker-supplied URL, and fall back to FRONTEND_URL otherwise."""

from src.config import config
from src.routes.auth import _resolve_frontend_base


def test_allowed_origin_is_honoured(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_FRONTEND_URLS", ["https://chat.libertai.io", "https://console.libertai.io"])
    monkeypatch.setattr(config, "FRONTEND_URL", "https://console.libertai.io")

    assert _resolve_frontend_base("https://chat.libertai.io") == "https://chat.libertai.io"
    # Trailing slash is normalised away.
    assert _resolve_frontend_base("https://chat.libertai.io/") == "https://chat.libertai.io"


def test_disallowed_origin_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_FRONTEND_URLS", ["https://chat.libertai.io"])
    monkeypatch.setattr(config, "FRONTEND_URL", "https://console.libertai.io")

    # An attacker-supplied origin must NOT be used — fall back to the default frontend.
    assert _resolve_frontend_base("https://evil.example.com") == "https://console.libertai.io"


def test_missing_origin_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_FRONTEND_URLS", ["https://chat.libertai.io"])
    monkeypatch.setattr(config, "FRONTEND_URL", "https://console.libertai.io")

    assert _resolve_frontend_base(None) == "https://console.libertai.io"
