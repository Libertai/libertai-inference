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
    source: str  # "tier" | "prepaid" | "team" | "blocked"
    # When each active window resets (None if no active window of that kind).
    window_5h_resets_at: datetime | None
    weekly_resets_at: datetime | None


@dataclass
class TeamExtraContext:
    team_id: uuid.UUID
    available: float


def compute_source(
    tier: TierConfig,
    usage_5h: float,
    usage_weekly: float,
    prepaid: float,
    team_extra_available: float = 0.0,
) -> str:
    """Decide which path covers the *next* call: tier window, prepaid, team extra, or none."""
    within_window = usage_5h < tier.window_5h_credits and usage_weekly < tier.weekly_credits
    if within_window:
        return "tier"
    if prepaid >= PREPAID_MIN:
        return "prepaid"
    if team_extra_available >= PREPAID_MIN:
        return "team"
    return "blocked"


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1)


async def _team_extra_spend(
    db: AsyncSession, team_id: uuid.UUID, month_start: datetime, user_id: uuid.UUID | None = None
) -> float:
    """Month-to-date team-funded spend: SUM(credits_used - tier_credits_used) over
    team-stamped calls. NEVER sum credits_used alone — the window-covered portion
    of a straddling call must not eat the cap."""
    stmt = select(
        sql_func.coalesce(
            sql_func.sum(InferenceCall.credits_used - InferenceCall.tier_credits_used), 0.0
        )
    ).where(InferenceCall.team_id == team_id, InferenceCall.used_at >= month_start)
    if user_id is not None:
        stmt = stmt.join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id).where(
            ApiKeyDB.user_id == user_id
        )
    return float((await db.execute(stmt)).scalar() or 0.0)


async def get_team_extra_context(
    db: AsyncSession, user_id: uuid.UUID, now: datetime
) -> TeamExtraContext | None:
    """Extra-credits headroom for a team member (None = not a member).

    available = max(0, min(member cap headroom, team cap headroom, team balance)).
    Caps of None mean 0 (disabled). Suspended teams always yield 0.
    """
    from src.models.team import Team
    from src.models.team_membership import TeamMembership
    from src.services.team_credit import TeamCreditService

    row = (
        await db.execute(
            select(TeamMembership, Team)
            .join(Team, Team.id == TeamMembership.team_id)
            .where(TeamMembership.user_id == user_id)
        )
    ).first()
    if row is None:
        return None
    membership, team = row
    if team.status != "active":
        return TeamExtraContext(team_id=team.id, available=0.0)

    member_cap = (
        membership.extra_credits_cap_override
        if membership.extra_credits_cap_override is not None
        else (team.extra_credits_member_default_cap or 0.0)
    )
    team_cap = team.extra_credits_monthly_cap or 0.0
    month_start = _month_start(now)
    member_spent = await _team_extra_spend(db, team.id, month_start, user_id=user_id)
    team_spent = await _team_extra_spend(db, team.id, month_start)
    balance = await TeamCreditService.get_balance(db, team.id)
    # Caps are best-effort under concurrency: spend is read without locks, so N members racing can collectively overshoot by up to (N-1) calls' overflow, bounded by the team balance.
    available = max(0.0, min(member_cap - member_spent, team_cap - team_spent, balance))
    return TeamExtraContext(team_id=team.id, available=available)


async def team_extra_available_by_users(
    db: AsyncSession, user_ids: set[uuid.UUID], now: datetime
) -> dict[uuid.UUID, TeamExtraContext]:
    """Batched ``get_team_extra_context`` (gateway path — no per-key N+1)."""
    from src.models.team import Team
    from src.models.team_credit_transaction import TeamCreditTransaction
    from src.models.team_membership import TeamMembership

    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(TeamMembership, Team)
            .join(Team, Team.id == TeamMembership.team_id)
            .where(TeamMembership.user_id.in_(user_ids))
        )
    ).all()
    if not rows:
        return {}
    team_ids = {team.id for _, team in rows}
    month_start = _month_start(now)

    member_spend_rows = (
        await db.execute(
            select(
                ApiKeyDB.user_id,
                sql_func.coalesce(
                    sql_func.sum(InferenceCall.credits_used - InferenceCall.tier_credits_used), 0.0
                ),
            )
            .join(InferenceCall, InferenceCall.api_key_id == ApiKeyDB.id)
            .where(
                InferenceCall.team_id.in_(team_ids),
                InferenceCall.used_at >= month_start,
                ApiKeyDB.user_id.in_(user_ids),
            )
            .group_by(ApiKeyDB.user_id)
        )
    ).all()
    member_spent = {r[0]: float(r[1]) for r in member_spend_rows}

    team_spend_rows = (
        await db.execute(
            select(
                InferenceCall.team_id,
                sql_func.coalesce(
                    sql_func.sum(InferenceCall.credits_used - InferenceCall.tier_credits_used), 0.0
                ),
            )
            .where(InferenceCall.team_id.in_(team_ids), InferenceCall.used_at >= month_start)
            .group_by(InferenceCall.team_id)
        )
    ).all()
    team_spent = {r[0]: float(r[1]) for r in team_spend_rows}

    balance_rows = (
        await db.execute(
            select(
                TeamCreditTransaction.team_id,
                sql_func.coalesce(sql_func.sum(TeamCreditTransaction.amount_left), 0.0),
            )
            .where(
                TeamCreditTransaction.team_id.in_(team_ids),
                TeamCreditTransaction.is_active == True,  # noqa: E712
                TeamCreditTransaction.status == CreditTransactionStatus.completed,
            )
            .group_by(TeamCreditTransaction.team_id)
        )
    ).all()
    balances = {r[0]: float(r[1]) for r in balance_rows}

    result: dict[uuid.UUID, TeamExtraContext] = {}
    for membership, team in rows:
        if team.status != "active":
            result[membership.user_id] = TeamExtraContext(team_id=team.id, available=0.0)
            continue
        member_cap = (
            membership.extra_credits_cap_override
            if membership.extra_credits_cap_override is not None
            else (team.extra_credits_member_default_cap or 0.0)
        )
        team_cap = team.extra_credits_monthly_cap or 0.0
        available = max(
            0.0,
            min(
                member_cap - member_spent.get(membership.user_id, 0.0),
                team_cap - team_spent.get(team.id, 0.0),
                balances.get(team.id, 0.0),
            ),
        )
        result[membership.user_id] = TeamExtraContext(team_id=team.id, available=available)
    return result


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

    ctx = await get_team_extra_context(db, user_id, now)
    if ctx is not None:
        # Members never spend personal prepaid — the team extra path funds overflow.
        source = compute_source(tier, usage_5h, usage_weekly, 0.0, team_extra_available=ctx.available)
    else:
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
