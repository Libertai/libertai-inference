"""Teams: creation (staff), membership, roles and the guards around them.

Seat billing lives in ``payments/team_seat_subscription.py``; invites in this
module too (Task 5). All methods flush only — the caller commits.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.team import Team
from src.models.team_membership import ROLE_ADMIN, ROLE_MEMBER, TeamMembership
from src.services.payments.team_seat_subscription import TEAM_CREDITS_PROVIDER
from src.subscription_tiers import PAID_TIERS, get_tier
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


async def expire_seat_now(
    db: AsyncSession, user_id: uuid.UUID, reason: str, metadata: dict | None = None
) -> None:
    """Expire the user's active team seat immediately (removal/leave/suspension).

    Remaining paid time is intentionally lost (spec: no refund to balance).
    """
    seat = (
        await db.execute(
            select(PlanSubscription)
            .where(
                PlanSubscription.user_id == user_id,
                PlanSubscription.provider == TEAM_CREDITS_PROVIDER,
                PlanSubscription.status == "active",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if seat is None:
        return
    seat.status = "expired"
    db.add(
        PlanSubscriptionEvent(
            subscription_id=seat.id, event_type=reason, provider_event_id=None, metadata_json=metadata
        )
    )
    await db.flush()


class TeamService:
    @staticmethod
    async def create_team(
        db: AsyncSession,
        name: str,
        seat_prices: dict[str, float] | None = None,
        extra_credits_monthly_cap: float | None = None,
        extra_credits_member_default_cap: float | None = None,
    ) -> Team:
        seat_prices = seat_prices or {}
        for tier, price in seat_prices.items():
            if tier not in PAID_TIERS:
                raise ValueError(f"Unknown tier in seat_prices: {tier}")
            if not isinstance(price, (int, float)) or price <= 0:
                raise ValueError(f"Seat price for {tier} must be positive")
        team = Team(name=name)
        team.seat_prices = seat_prices
        team.extra_credits_monthly_cap = extra_credits_monthly_cap
        team.extra_credits_member_default_cap = extra_credits_member_default_cap
        db.add(team)
        await db.flush()
        return team

    @staticmethod
    def seat_price(team: Team, tier: str) -> float:
        """Negotiated monthly USD price for a tier; list price when no deal is configured."""
        if team.seat_prices:
            price = team.seat_prices.get(tier)
            if price is None:
                raise ValueError(f"Tier {tier!r} is not sold to this team")
            return float(price)
        return get_tier(tier).price_cents / 100

    @staticmethod
    async def get_membership(db: AsyncSession, user_id: uuid.UUID) -> TeamMembership | None:
        return (
            await db.execute(select(TeamMembership).where(TeamMembership.user_id == user_id))
        ).scalar_one_or_none()

    @staticmethod
    async def require_membership(
        db: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID, admin: bool = False
    ) -> TeamMembership:
        membership = await TeamService.get_membership(db, user_id)
        if membership is None or membership.team_id != team_id:
            raise PermissionError("Not a member of this team")
        if admin and membership.role != ROLE_ADMIN:
            raise PermissionError("Requires team admin role")
        return membership

    @staticmethod
    async def _admin_count(db: AsyncSession, team_id: uuid.UUID) -> int:
        return (
            await db.execute(
                select(func.count()).where(
                    TeamMembership.team_id == team_id, TeamMembership.role == ROLE_ADMIN
                )
            )
        ).scalar_one()

    @staticmethod
    async def _guard_not_last_admin(db: AsyncSession, membership: TeamMembership) -> None:
        if membership.role == ROLE_ADMIN and await TeamService._admin_count(db, membership.team_id) <= 1:
            raise ValueError("Cannot remove or demote the last admin of a team")

    @staticmethod
    async def set_role(
        db: AsyncSession, team_id: uuid.UUID, actor_id: uuid.UUID, target_user_id: uuid.UUID, role: str
    ) -> None:
        # Defense-in-depth: routes also gate on admin, but the service must not trust callers.
        await TeamService.require_membership(db, team_id, actor_id, admin=True)
        if role not in (ROLE_ADMIN, ROLE_MEMBER):
            raise ValueError(f"Unknown role: {role}")
        target = await TeamService.require_membership(db, team_id, target_user_id)
        if role != ROLE_ADMIN:
            await TeamService._guard_not_last_admin(db, target)
        target.role = role
        await db.flush()

    @staticmethod
    async def remove_member(
        db: AsyncSession, team_id: uuid.UUID, target_user_id: uuid.UUID, removed_by: uuid.UUID | str
    ) -> None:
        """Delete the membership and expire any seat immediately.

        The remover is recorded in the seat event metadata because the
        membership row (and its audit trail) is deleted here.
        """
        target = await TeamService.require_membership(db, team_id, target_user_id)
        await TeamService._guard_not_last_admin(db, target)
        await expire_seat_now(
            db, target_user_id, "member_removed", metadata={"removed_by": str(removed_by)}
        )
        await db.delete(target)
        await db.flush()

    @staticmethod
    async def leave(db: AsyncSession, user_id: uuid.UUID) -> None:
        membership = await TeamService.get_membership(db, user_id)
        if membership is None:
            raise ValueError("Not a member of any team")
        await TeamService._guard_not_last_admin(db, membership)
        await expire_seat_now(db, user_id, "member_left", metadata={"removed_by": str(user_id)})
        await db.delete(membership)
        await db.flush()
