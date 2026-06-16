"""Per-IP rate limit for anonymous (logged-out) chat.

Anonymous chat traffic flows through the POST /chat/completions proxy (shared key). To nudge
heavy users to sign in — and cap abuse of the free shared key — we count messages per client IP
in a fixed window. This is intentionally soft: keyed by IP only (anonymous users have no
account), so shared/NAT IPs share a window and VPN/IP rotation can bypass it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.anon_chat_usage import AnonChatUsage

ANON_MESSAGE_LIMIT = 10
ANON_WINDOW = timedelta(hours=24)


@dataclass
class AnonUsageState:
    used: int
    limit: int
    allowed: bool
    resets_at: datetime | None  # None when no window is active yet (used == 0)


async def consume(db: AsyncSession, ip: str, now: datetime | None = None) -> AnonUsageState:
    """Count one anonymous message against the IP's window and return the resulting state.

    When the IP is already at the limit, nothing is consumed and ``allowed`` is False — the
    caller should reject the request. Resets the window in place once it has expired.
    """
    now = now or datetime.now()
    # Ensure a row exists, then lock it so concurrent messages from the same IP serialise.
    await db.execute(
        pg_insert(AnonChatUsage)
        .values(id=uuid.uuid4(), ip=ip, window_started_at=now, count=0)
        .on_conflict_do_nothing(index_elements=["ip"])
    )
    row = (
        await db.execute(select(AnonChatUsage).where(AnonChatUsage.ip == ip).with_for_update())
    ).scalar_one()

    if row.window_started_at + ANON_WINDOW <= now:
        row.window_started_at = now
        row.count = 0

    allowed = row.count < ANON_MESSAGE_LIMIT
    if allowed:
        row.count += 1
    await db.commit()
    return AnonUsageState(
        used=row.count,
        limit=ANON_MESSAGE_LIMIT,
        allowed=allowed,
        resets_at=row.window_started_at + ANON_WINDOW,
    )


async def get_state(db: AsyncSession, ip: str, now: datetime | None = None) -> AnonUsageState:
    """Read-only view of an IP's current anonymous usage (for the frontend meter)."""
    now = now or datetime.now()
    row = (await db.execute(select(AnonChatUsage).where(AnonChatUsage.ip == ip))).scalar_one_or_none()
    if row is None or row.window_started_at + ANON_WINDOW <= now:
        return AnonUsageState(used=0, limit=ANON_MESSAGE_LIMIT, allowed=True, resets_at=None)
    return AnonUsageState(
        used=row.count,
        limit=ANON_MESSAGE_LIMIT,
        allowed=row.count < ANON_MESSAGE_LIMIT,
        resets_at=row.window_started_at + ANON_WINDOW,
    )
