"""Per-IP and per-network fixed-window limiter for email sign-in/sign-up.

Domain blocklists lose to custom-domain rotation, so this bounds attempts by source address
instead. Each request consumes one slot in a daily per-IP window and one in its network's
window (IPv4 /24, IPv6 /64); either can reject. The row is locked while it is updated, so
concurrent requests from the same IP or network serialise.
"""

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import config
from src.models.signup_rate_limit import SignupRateLimit


@dataclass(frozen=True)
class SignupRateState:
    allowed: bool
    retry_at: datetime | None  # only set when rejected


def client_ip(request: Request) -> str | None:
    """The client address Traefik appended to X-Forwarded-For.

    Traefik appends the peer address to any client-supplied list, so the RIGHTMOST entry is
    the one it vouches for. The leftmost entry is attacker-controlled and must not be used to
    key a rate limit.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip() or None
    return request.client.host if request.client else None


def network_for(ip: str) -> str | None:
    """The /24 (IPv4) or /64 (IPv6) containing ``ip``; None when it is not an address."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


async def _consume_scope(db: AsyncSession, scope: str, value: str, limit: int, now: datetime) -> datetime | None:
    """Count one attempt and return the retry time when this scope is over its limit."""
    await db.execute(
        pg_insert(SignupRateLimit)
        .values(id=uuid.uuid4(), scope=scope, value=value, window_started_at=now, count=0)
        .on_conflict_do_nothing(index_elements=["scope", "value"])
    )
    row = (
        (
            await db.execute(
                select(SignupRateLimit)
                .where(SignupRateLimit.scope == scope, SignupRateLimit.value == value)
                .with_for_update()
            )
        )
        .scalars()
        .one()
    )
    if row.window_started_at + config.SIGNUP_RATE_WINDOW <= now:
        row.window_started_at = now
        row.count = 0

    if row.count >= limit:
        return row.window_started_at + config.SIGNUP_RATE_WINDOW

    row.count += 1
    return None


async def consume(db: AsyncSession, ip: str, now: datetime | None = None) -> SignupRateState:
    """Count one attempt against the IP and network windows. Commits its own transaction."""
    now = now or datetime.now()
    retry_at: datetime | None = None

    retry = await _consume_scope(db, "ip", ip, config.SIGNUP_RATE_IP_LIMIT, now)
    retry_at = retry or retry_at

    network = network_for(ip)
    if network is not None:
        retry = await _consume_scope(db, "net", network, config.SIGNUP_RATE_NETWORK_LIMIT, now)
        retry_at = retry or retry_at

    await db.commit()
    return SignupRateState(allowed=retry_at is None, retry_at=retry_at)


def retry_after_seconds(state: SignupRateState, now: datetime | None = None) -> int:
    """Whole seconds until the rejected window expires (0 if it is somehow in the past)."""
    if state.retry_at is None:
        return 0
    now = now or datetime.now()
    return max(0, int((state.retry_at - now).total_seconds()))
