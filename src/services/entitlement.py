"""Dual-window entitlement: a trailing-5h and trailing-7d credit allowance.

Every user gets the ``free`` tier's windows by default; an active paid
subscription grants larger windows. When a window is exhausted, usage falls
through to prepaid balance (top-ups / on-chain credits). A user is allowed to
make inference calls while *either* path has room.

This is the single source of truth consumed by both the gateway chokepoint
(``api_key.get_admin_all_api_keys`` / ``register_inference_call``) and the
``/payments/subscription`` window display.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func as sql_func

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionStatus
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.credit_transaction import CreditTransaction
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.subscription_tiers import DEFAULT_TIER, TierConfig, get_tier

# Minimum prepaid balance required to cover an inference call once tier windows
# are exhausted (matches the legacy gateway threshold).
PREPAID_MIN = 0.02

WINDOW_5H = timedelta(hours=5)
WINDOW_WEEKLY = timedelta(days=7)


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
    resets_at: datetime | None  # when the binding tier window next frees up


def compute_source(tier: TierConfig, usage_5h: float, usage_weekly: float, prepaid: float) -> str:
    """Decide which path covers the *next* call: tier window, prepaid, or none."""
    within_window = usage_5h < tier.window_5h_credits and usage_weekly < tier.weekly_credits
    if within_window:
        return "tier"
    if prepaid >= PREPAID_MIN:
        return "prepaid"
    return "blocked"


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
    """Credits used across a user's ``api`` keys since ``cutoff``."""
    total = (
        await db.execute(
            select(sql_func.coalesce(sql_func.sum(InferenceCall.credits_used), 0.0))
            .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
            .where(
                ApiKeyDB.user_id == user_id,
                ApiKeyDB.type == ApiKeyType.api,
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


async def get_allowance_state(
    db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None
) -> AllowanceState:
    now = now or datetime.now()
    tier = get_tier(await get_active_tier(db, user_id))

    usage_5h = await _usage_since(db, user_id, now - WINDOW_5H)
    usage_weekly = await _usage_since(db, user_id, now - WINDOW_WEEKLY)
    prepaid = await _prepaid_balance(db, user_id)

    source = compute_source(tier, usage_5h, usage_weekly, prepaid)
    # Best-effort reset: the 5h window is the most frequently binding one.
    resets_at = now + WINDOW_5H if source != "tier" else None

    return AllowanceState(
        allowed=source != "blocked",
        tier=tier.name,
        window_5h_used=usage_5h,
        window_5h_limit=tier.window_5h_credits,
        weekly_used=usage_weekly,
        weekly_limit=tier.weekly_credits,
        prepaid_balance=prepaid,
        source=source,
        resets_at=resets_at,
    )
