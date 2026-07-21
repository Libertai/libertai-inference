"""Per-IP anonymous chat rate limit (service + proxy enforcement + usage endpoint)."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete

from src.models.anon_chat_usage import AnonChatUsage
from src.models.base import AsyncSessionLocal
from src.services import anon_rate_limit
from src.services.anon_rate_limit import (
    ANON_MESSAGE_LIMIT,
    ANON_WEEK_MESSAGE_LIMIT,
    ANON_WEEK_WINDOW,
    ANON_WINDOW,
)

pytestmark = pytest.mark.asyncio


async def _cleanup(ip: str):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(AnonChatUsage).where(AnonChatUsage.ip == ip))
        await db.commit()


async def test_consume_counts_up_then_blocks_at_limit():
    ip = "203.0.113.10"
    try:
        async with AsyncSessionLocal() as db:
            for i in range(1, ANON_MESSAGE_LIMIT + 1):
                state = await anon_rate_limit.consume(db, ip)
                assert state.used == i
                assert state.allowed is True
            # One past the limit: nothing consumed, blocked.
            blocked = await anon_rate_limit.consume(db, ip)
            assert blocked.allowed is False
            assert blocked.used == ANON_MESSAGE_LIMIT  # unchanged
    finally:
        await _cleanup(ip)


async def test_window_resets_after_expiry():
    ip = "203.0.113.11"
    t0 = datetime(2026, 6, 16, 12, 0, 0)
    try:
        async with AsyncSessionLocal() as db:
            for _ in range(ANON_MESSAGE_LIMIT):
                await anon_rate_limit.consume(db, ip, now=t0)
            assert (await anon_rate_limit.consume(db, ip, now=t0)).allowed is False
            # After the window passes, the counter resets and messages flow again.
            after = await anon_rate_limit.consume(db, ip, now=t0 + ANON_WINDOW + timedelta(minutes=1))
            assert after.allowed is True
            assert after.used == 1
    finally:
        await _cleanup(ip)


async def test_weekly_cap_blocks_across_daily_windows():
    ip = "203.0.113.13"
    t0 = datetime(2026, 6, 16, 12, 0, 0)
    try:
        async with AsyncSessionLocal() as db:
            # Burn the daily limit on consecutive days until the weekly cap is hit.
            days_to_cap = ANON_WEEK_MESSAGE_LIMIT // ANON_MESSAGE_LIMIT
            for day in range(days_to_cap):
                for _ in range(ANON_MESSAGE_LIMIT):
                    state = await anon_rate_limit.consume(db, ip, now=t0 + timedelta(days=day))
                    assert state.allowed is True
            # Next day: daily window is fresh but the weekly cap now blocks.
            blocked = await anon_rate_limit.consume(db, ip, now=t0 + timedelta(days=days_to_cap))
            assert blocked.allowed is False
            assert blocked.limit == ANON_WEEK_MESSAGE_LIMIT
            assert blocked.used == ANON_WEEK_MESSAGE_LIMIT
            # Once the weekly window expires, messages flow again.
            after = await anon_rate_limit.consume(db, ip, now=t0 + ANON_WEEK_WINDOW + timedelta(minutes=1))
            assert after.allowed is True
            assert after.used == 1 and after.limit == ANON_MESSAGE_LIMIT
    finally:
        await _cleanup(ip)


async def test_state_reports_weekly_window_when_binding():
    ip = "203.0.113.14"
    t0 = datetime(2026, 6, 16, 12, 0, 0)
    try:
        async with AsyncSessionLocal() as db:
            # 2 messages on day 0, then 3 full days: weekly total is 2 + 3*limit.
            for _ in range(2):
                await anon_rate_limit.consume(db, ip, now=t0)
            for day in range(1, 4):
                for _ in range(ANON_MESSAGE_LIMIT):
                    await anon_rate_limit.consume(db, ip, now=t0 + timedelta(days=day))
            # On day 4 the weekly remainder is strictly tighter than the fresh daily window.
            used_week = 2 + 3 * ANON_MESSAGE_LIMIT + 1
            state = await anon_rate_limit.consume(db, ip, now=t0 + timedelta(days=4))
            assert state.limit == ANON_WEEK_MESSAGE_LIMIT
            assert state.used == used_week
        async with AsyncSessionLocal() as db:
            read = await anon_rate_limit.get_state(db, ip, now=t0 + timedelta(days=4))
            assert read.limit == ANON_WEEK_MESSAGE_LIMIT
            assert read.used == used_week
    finally:
        await _cleanup(ip)


async def test_get_state_is_read_only():
    ip = "203.0.113.12"
    try:
        async with AsyncSessionLocal() as db:
            await anon_rate_limit.consume(db, ip)
            await anon_rate_limit.consume(db, ip)
        async with AsyncSessionLocal() as db:
            s1 = await anon_rate_limit.get_state(db, ip)
            s2 = await anon_rate_limit.get_state(db, ip)
        assert s1.used == 2 and s2.used == 2  # reading doesn't increment
        assert s1.allowed is True
        assert s1.resets_at is not None
    finally:
        await _cleanup(ip)


async def test_get_state_unknown_ip_is_fresh():
    async with AsyncSessionLocal() as db:
        s = await anon_rate_limit.get_state(db, "203.0.113.250")
    assert s.used == 0 and s.allowed is True and s.resets_at is None


async def test_proxy_returns_429_when_limit_reached(async_client):
    ip = "203.0.113.20"
    try:
        async with AsyncSessionLocal() as db:
            for _ in range(ANON_MESSAGE_LIMIT):
                await anon_rate_limit.consume(db, ip)
        # The 11th message is rejected before the proxy forwards anything upstream.
        resp = await async_client.post(
            "/chat/completions",
            json={"model": "x", "messages": []},
            headers={"x-forwarded-for": ip},
        )
        assert resp.status_code == 429, resp.text
        body = resp.json()
        assert body["detail"] == "anon_limit"
        assert body["limit"] == ANON_MESSAGE_LIMIT
        assert body["allowed"] is False
    finally:
        await _cleanup(ip)


async def test_anon_usage_endpoint_reports_state(async_client):
    ip = "203.0.113.21"
    try:
        async with AsyncSessionLocal() as db:
            await anon_rate_limit.consume(db, ip)
        r = await async_client.get("/chat/anon-usage", headers={"x-forwarded-for": ip})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["used"] == 1
        assert j["limit"] == ANON_MESSAGE_LIMIT
        assert j["allowed"] is True
        assert j["resets_at"] is not None
    finally:
        await _cleanup(ip)
