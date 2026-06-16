import asyncio
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

DAY_SECONDS = 24 * 60 * 60
MINUTE_SECONDS = 60


@dataclass(frozen=True)
class RateLimitRule:
    scope: str
    limit: int
    window_seconds: int


@dataclass
class _Counter:
    count: int
    resets_at: float


_lock = asyncio.Lock()
_counters: dict[tuple[str, str], _Counter] = {}


def client_identity(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


async def _increment_or_raise(identity: str, rule: RateLimitRule, now: float) -> None:
    if rule.limit <= 0:
        return

    key = (rule.scope, identity)
    async with _lock:
        counter = _counters.get(key)
        if counter is None or counter.resets_at <= now:
            counter = _Counter(count=0, resets_at=now + rule.window_seconds)
            _counters[key] = counter

        counter.count += 1
        if counter.count <= rule.limit:
            return

        retry_after = max(1, int(counter.resets_at - now))

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": "rate_limit_exceeded",
            "scope": rule.scope,
            "limit": rule.limit,
            "window_seconds": rule.window_seconds,
            "retry_after": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


async def enforce_free_chat_rate_limit(request: Request, *, now: float | None = None) -> None:
    from src.config import config

    identity = client_identity(request)
    checked_at = time.time() if now is None else now
    rules = [
        RateLimitRule("free_chat_requests_per_minute", config.FREE_CHAT_RATE_LIMIT_PER_MINUTE, MINUTE_SECONDS),
        RateLimitRule("free_chat_requests_per_day", config.FREE_CHAT_RATE_LIMIT_PER_DAY, DAY_SECONDS),
    ]

    for rule in rules:
        await _increment_or_raise(identity, rule, checked_at)


async def reset_free_chat_rate_limits() -> None:
    async with _lock:
        _counters.clear()
