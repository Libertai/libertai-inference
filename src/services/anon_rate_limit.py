"""Per-IP rate limit for anonymous (logged-out) chat.

Anonymous chat traffic flows through the POST /chat/completions proxy (shared key). To nudge
heavy users to sign in — and cap abuse of the free shared key — we count messages per client IP
in two fixed windows: a daily one and a longer weekly one (so an IP can't just come back every
24h forever). This is intentionally soft: keyed by IP only (anonymous users have no
account), so shared/NAT IPs share a window and VPN/IP rotation can bypass it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.anon_chat_usage import AnonChatUsage

ANON_MESSAGE_LIMIT = 5
ANON_WINDOW = timedelta(hours=24)
ANON_WEEK_MESSAGE_LIMIT = 20
ANON_WEEK_WINDOW = timedelta(days=7)


@dataclass
class AnonUsageState:
    used: int
    limit: int
    allowed: bool
    resets_at: datetime | None  # None when no window is active yet (used == 0)


def _state(
    count: int, window_started_at: datetime, week_count: int, week_started_at: datetime, allowed: bool
) -> AnonUsageState:
    """State of the binding window: report weekly numbers only when the weekly cap is the
    tighter constraint, so the frontend meter/wall always shows the limit actually in play.

    ``allowed`` is decided by the caller: for ``consume`` it means "this message was accepted"
    (not "another one would be"), which is what the proxy checks before forwarding.
    """
    day_remaining = ANON_MESSAGE_LIMIT - count
    week_remaining = ANON_WEEK_MESSAGE_LIMIT - week_count
    if week_remaining < day_remaining:
        return AnonUsageState(
            used=week_count,
            limit=ANON_WEEK_MESSAGE_LIMIT,
            allowed=allowed,
            resets_at=week_started_at + ANON_WEEK_WINDOW,
        )
    return AnonUsageState(
        used=count,
        limit=ANON_MESSAGE_LIMIT,
        allowed=allowed,
        resets_at=window_started_at + ANON_WINDOW if count > 0 else None,
    )


async def consume(db: AsyncSession, ip: str, now: datetime | None = None) -> AnonUsageState:
    """Count one anonymous message against the IP's windows and return the resulting state.

    When the IP is at either limit (daily or weekly), nothing is consumed and ``allowed`` is
    False — the caller should reject the request. Resets each window in place once it expires.
    """
    now = now or datetime.now()
    # Ensure a row exists, then lock it so concurrent messages from the same IP serialise.
    await db.execute(
        pg_insert(AnonChatUsage)
        .values(id=uuid.uuid4(), ip=ip, window_started_at=now, count=0, week_started_at=now, week_count=0)
        .on_conflict_do_nothing(index_elements=["ip"])
    )
    row = (
        await db.execute(select(AnonChatUsage).where(AnonChatUsage.ip == ip).with_for_update())
    ).scalar_one()

    if row.window_started_at + ANON_WINDOW <= now:
        row.window_started_at = now
        row.count = 0
    if row.week_started_at + ANON_WEEK_WINDOW <= now:
        row.week_started_at = now
        row.week_count = 0

    allowed = row.count < ANON_MESSAGE_LIMIT and row.week_count < ANON_WEEK_MESSAGE_LIMIT
    if allowed:
        row.count += 1
        row.week_count += 1
    await db.commit()
    return _state(row.count, row.window_started_at, row.week_count, row.week_started_at, allowed)


async def get_state(db: AsyncSession, ip: str, now: datetime | None = None) -> AnonUsageState:
    """Read-only view of an IP's current anonymous usage (for the frontend meter)."""
    now = now or datetime.now()
    row = (await db.execute(select(AnonChatUsage).where(AnonChatUsage.ip == ip))).scalar_one_or_none()
    if row is None:
        return AnonUsageState(used=0, limit=ANON_MESSAGE_LIMIT, allowed=True, resets_at=None)
    # Expired windows count as empty (virtual reset — nothing is written on a read path).
    day_active = row.window_started_at + ANON_WINDOW > now
    week_active = row.week_started_at + ANON_WEEK_WINDOW > now
    count = row.count if day_active else 0
    week_count = row.week_count if week_active else 0
    return _state(
        count=count,
        window_started_at=row.window_started_at if day_active else now,
        week_count=week_count,
        week_started_at=row.week_started_at if week_active else now,
        allowed=count < ANON_MESSAGE_LIMIT and week_count < ANON_WEEK_MESSAGE_LIMIT,
    )
