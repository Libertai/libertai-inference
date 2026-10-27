"""Per-IP/network signup limits — the control that survives custom-domain rotation."""

from datetime import datetime, timedelta

import pytest
from starlette.requests import Request

from src.config import config
from src.services import signup_rate_limit

pytestmark = pytest.mark.asyncio


def test_network_for_ipv4_is_the_24():
    assert signup_rate_limit.network_for("1.2.3.4") == "1.2.3.0/24"


def test_network_for_ipv6_is_the_64():
    assert signup_rate_limit.network_for("2001:db8:abcd:1234::1") == "2001:db8:abcd:1234::/64"


def test_network_for_invalid_ip_is_none():
    assert signup_rate_limit.network_for("not-an-ip") is None


def test_client_ip_uses_the_rightmost_forwarded_entry():
    """Traefik appends the peer it saw; the leftmost entry is client-supplied and spoofable."""
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"1.1.1.1, 203.0.113.9")],
            "client": ("10.0.0.5", 1234),
        }
    )
    assert signup_rate_limit.client_ip(request) == "203.0.113.9"


async def test_ip_limit_rejects_after_the_allowance(db, monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_RATE_IP_LIMIT", 2)
    monkeypatch.setattr(config, "SIGNUP_RATE_NETWORK_LIMIT", 100)
    now = datetime(2026, 10, 27, 12, 0, 0)

    assert (await signup_rate_limit.consume(db, "203.0.113.9", now)).allowed is True
    assert (await signup_rate_limit.consume(db, "203.0.113.9", now)).allowed is True
    state = await signup_rate_limit.consume(db, "203.0.113.9", now)
    assert state.allowed is False
    assert state.retry_at == now + config.SIGNUP_RATE_WINDOW
    assert signup_rate_limit.retry_after_seconds(state, now) == int(config.SIGNUP_RATE_WINDOW.total_seconds())


async def test_network_limit_spans_different_ips_in_the_same_24(db, monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_RATE_IP_LIMIT", 100)
    monkeypatch.setattr(config, "SIGNUP_RATE_NETWORK_LIMIT", 2)
    now = datetime(2026, 10, 27, 12, 0, 0)

    assert (await signup_rate_limit.consume(db, "203.0.113.1", now)).allowed is True
    assert (await signup_rate_limit.consume(db, "203.0.113.2", now)).allowed is True
    assert (await signup_rate_limit.consume(db, "203.0.113.3", now)).allowed is False


async def test_a_new_window_allows_again(db, monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_RATE_IP_LIMIT", 1)
    monkeypatch.setattr(config, "SIGNUP_RATE_NETWORK_LIMIT", 100)
    now = datetime(2026, 10, 27, 12, 0, 0)

    assert (await signup_rate_limit.consume(db, "203.0.113.9", now)).allowed is True
    assert (await signup_rate_limit.consume(db, "203.0.113.9", now)).allowed is False
    later = now + timedelta(seconds=1) + config.SIGNUP_RATE_WINDOW
    assert (await signup_rate_limit.consume(db, "203.0.113.9", later)).allowed is True
