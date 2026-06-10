"""Dual fixed-window entitlement: a 5-hour and a weekly allowance that reset on expiry.

Each window is **fixed**, not rolling: it opens when the user sends a message (via
``open_windows``), accumulates usage against the tier limit for a fixed duration,
then resets to empty all at once when it expires — the next message opens a fresh
window. (Contrast with a rolling window, where old usage gradually ages out.)

Every user gets the ``free`` tier's windows by default; an active paid subscription
grants larger ones. When a window is exhausted, usage falls through to prepaid
balance. A user is allowed while *either* path has room.

This is the single source of truth consumed by both the gateway chokepoint
(``api_key.get_admin_all_api_keys`` / ``register_inference_call``) and the
``/payments/subscription`` window display.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func as sql_func

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionStatus
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.credit_transaction import CreditTransaction
from src.models.entitlement_window import EntitlementWindow
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.subscription_tiers import DEFAULT_TIER, TierConfig, get_tier

# Minimum prepaid balance required to cover an inference call once tier windows
# are exhausted (matches the legacy gateway threshold).
PREPAID_MIN = 0.02

# Fixed window kinds and their durations.
WINDOW_5H = "5h"
WINDOW_WEEKLY = "weekly"
WINDOW_DURATIONS: dict[str, timedelta] = {
    WINDOW_5H: timedelta(hours=5),
    WINDOW_WEEKLY: timedelta(days=7),
}

# Key types whose usage accrues against a user's entitlement windows + prepaid balance.
# Excludes liberclaw and x402 (separate billing paths); the shared anonymous chat
# service key is excluded by value in register_inference_call, not by type here.
CHARGEABLE_KEY_TYPES = (ApiKeyType.api, ApiKeyType.cli, ApiKeyType.chat)


@dataclass
class AllowanceState:
    allowed: bool
    tier: str
    window_5h_used: float
    window_5h_limit: float
    weekly_used: float
    weekly_limit: float
    prepaid_balance: float
    source: str  # "tier" | "prepaid" | "blocked"
    # When each active window resets (None if no active window of that kind).
    window_5h_resets_at: datetime | None
    weekly_resets_at: datetime | None


def compute_source(tier: TierConfig, usage_5h: float, usage_weekly: float, prepaid: float) -> str:
    """Decide which path covers the *next* call: tier window, prepaid, or none."""
    within_window = usage_5h < tier.window_5h_credits and usage_weekly < tier.weekly_credits
    if within_window:
        return "tier"
    if prepaid >= PREPAID_MIN:
        return "prepaid"
    return "blocked"


async def open_windows(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> None:
    """Ensure both fixed windows are open for a user sending a message.

    Creates a window if none exists, or resets an expired one to start now. Active
    windows are left untouched. Flushes; the caller controls the commit.

    Creation is an upsert: two concurrent first-ever calls would otherwise both see
    "no row" (nothing to lock yet) and one would die on the unique constraint,
    rolling back its usage row. The subsequent FOR UPDATE select also serializes
    concurrent billing splits for the same user.
    """
    now = now or datetime.now()
    for kind, duration in WINDOW_DURATIONS.items():
        await db.execute(
            pg_insert(EntitlementWindow)
            .values(id=uuid.uuid4(), user_id=user_id, kind=kind, started_at=now, expires_at=now + duration)
            .on_conflict_do_nothing(constraint="uq_entitlement_window_user_kind")
        )
        window = (
            await db.execute(
                select(EntitlementWindow)
                .where(EntitlementWindow.user_id == user_id, EntitlementWindow.kind == kind)
                .with_for_update()
            )
        ).scalar_one()
        if window.expires_at <= now:
            window.started_at = now
            window.expires_at = now + duration
        # else: still active (or just created) — leave it.
    await db.flush()


async def _active_window(db: AsyncSession, user_id: uuid.UUID, kind: str, now: datetime) -> EntitlementWindow | None:
    window = (
        await db.execute(
            select(EntitlementWindow).where(EntitlementWindow.user_id == user_id, EntitlementWindow.kind == kind)
        )
    ).scalar_one_or_none()
    return window if window and window.expires_at > now else None


async def get_active_tier(db: AsyncSession, user_id: uuid.UUID) -> str:
    sub = (
        await db.execute(
            select(PlanSubscription.tier).where(
                PlanSubscription.user_id == user_id,
                PlanSubscription.status == "active",
            )
        )
    ).scalars().first()
    return sub or DEFAULT_TIER


async def _usage_since(db: AsyncSession, user_id: uuid.UUID, cutoff: datetime) -> float:
    """Tier-subsidized credits across a user's chargeable keys (api, cli, chat) since ``cutoff``.

    Sums ``tier_credits_used`` (not ``credits_used``): the portion of a call paid from
    prepaid balance must not drain the window allowance.
    """
    total = (
        await db.execute(
            select(sql_func.coalesce(sql_func.sum(InferenceCall.tier_credits_used), 0.0))
            .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
            .where(
                ApiKeyDB.user_id == user_id,
                ApiKeyDB.type.in_(CHARGEABLE_KEY_TYPES),
                InferenceCall.used_at >= cutoff,
            )
        )
    ).scalar()
    return float(total or 0.0)


async def _prepaid_balance(db: AsyncSession, user_id: uuid.UUID) -> float:
    total = (
        await db.execute(
            select(sql_func.coalesce(sql_func.sum(CreditTransaction.amount_left), 0.0)).where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.is_active == True,  # noqa: E712
                CreditTransaction.status == CreditTransactionStatus.completed,
            )
        )
    ).scalar()
    return float(total or 0.0)


async def window_usage_by_users(
    db: AsyncSession, user_ids: set[uuid.UUID], kind: str, now: datetime
) -> dict[uuid.UUID, float]:
    """Batched tier-subsidized usage within each user's *active* window of ``kind``
    (avoids N+1 at the gateway). Sums ``tier_credits_used`` — prepaid-paid usage
    doesn't drain the allowance.

    Users with no active window are absent from the result (treated as 0 used).
    """
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(
                ApiKeyDB.user_id,
                sql_func.coalesce(sql_func.sum(InferenceCall.tier_credits_used), 0.0),
            )
            .join(InferenceCall, InferenceCall.api_key_id == ApiKeyDB.id)
            .join(
                EntitlementWindow,
                and_(
                    EntitlementWindow.user_id == ApiKeyDB.user_id,
                    EntitlementWindow.kind == kind,
                ),
            )
            .where(
                ApiKeyDB.user_id.in_(user_ids),
                ApiKeyDB.type.in_(CHARGEABLE_KEY_TYPES),
                EntitlementWindow.expires_at > now,
                InferenceCall.used_at >= EntitlementWindow.started_at,
            )
            .group_by(ApiKeyDB.user_id)
        )
    ).all()
    return {row[0]: float(row[1]) for row in rows}


async def active_tiers_by_users(db: AsyncSession, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Batched active-subscription tier per user (absent => free)."""
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(PlanSubscription.user_id, PlanSubscription.tier).where(
                PlanSubscription.user_id.in_(user_ids),
                PlanSubscription.status == "active",
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def get_allowance_state(
    db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None
) -> AllowanceState:
    now = now or datetime.now()
    tier = get_tier(await get_active_tier(db, user_id))

    window_5h = await _active_window(db, user_id, WINDOW_5H, now)
    window_weekly = await _active_window(db, user_id, WINDOW_WEEKLY, now)
    usage_5h = await _usage_since(db, user_id, window_5h.started_at) if window_5h else 0.0
    usage_weekly = await _usage_since(db, user_id, window_weekly.started_at) if window_weekly else 0.0
    prepaid = await _prepaid_balance(db, user_id)

    source = compute_source(tier, usage_5h, usage_weekly, prepaid)

    return AllowanceState(
        allowed=source != "blocked",
        tier=tier.name,
        window_5h_used=usage_5h,
        window_5h_limit=tier.window_5h_credits,
        weekly_used=usage_weekly,
        weekly_limit=tier.weekly_credits,
        prepaid_balance=prepaid,
        source=source,
        window_5h_resets_at=window_5h.expires_at if window_5h else None,
        weekly_resets_at=window_weekly.expires_at if window_weekly else None,
    )
