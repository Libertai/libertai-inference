"""Team seat subscriptions: PlanSubscription rows paid from the team balance.

Calendar-month aligned (naive ``datetime.now()``, codebase convention): a seat
assigned mid-month is charged ``price * remaining_days / days_in_month``
(remaining includes today) and every seat renews on the 1st via
``process_renewals`` — one aggregate debit per team, all-or-nothing.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.team import Team
from src.models.team_membership import ROLE_ADMIN, TeamMembership
from src.models.user import User
from src.services.team_credit import TeamCreditService
from src.subscription_tiers import DEFAULT_TIER, PAID_TIERS, is_downgrade, is_upgrade
from src.utils.email import send_email
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

TEAM_CREDITS_PROVIDER = "team_credits"


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, 1)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
    return start, end


def prorated_price(price: float, now: datetime) -> float:
    """Charge for the rest of the month, today included (1st = full price)."""
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    remaining_days = days_in_month - now.day + 1
    return round(price * remaining_days / days_in_month, 2)


def _log(db: AsyncSession, sub: PlanSubscription, event_type: str, metadata: dict | None = None) -> None:
    db.add(
        PlanSubscriptionEvent(
            subscription_id=sub.id, event_type=event_type, provider_event_id=None, metadata_json=metadata
        )
    )


async def _active_seat(db: AsyncSession, user_id: uuid.UUID) -> PlanSubscription | None:
    return (
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


class TeamSeatService:
    @staticmethod
    async def assign_seat(
        db: AsyncSession, team: Team, user_id: uuid.UUID, tier: str, now: datetime | None = None
    ) -> PlanSubscription:
        from src.services.teams import TeamService  # local import: teams.py imports our constant

        now = now or datetime.now()
        if team.status != "active":
            raise ValueError("Team is suspended")
        if tier not in PAID_TIERS:
            raise ValueError(f"Unknown or non-paid tier: {tier}")
        membership = (
            await db.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team.id, TeamMembership.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise ValueError("User is not a member of this team")
        if await _active_seat(db, user_id) is not None:
            raise ValueError("Member already has a seat")

        price = TeamService.seat_price(team, tier)
        charge = prorated_price(price, now)
        if not await TeamCreditService.use_credits(db, team.id, charge):
            raise ValueError("Insufficient team balance — top up first")
        await TeamCreditService.log(
            db, team.id, "seat_charge_prorated", charge,
            {"user_id": str(user_id), "tier": tier, "monthly_price": price},
        )

        _, period_end = month_bounds(now)
        seat = PlanSubscription(
            user_id=user_id,
            tier=tier,
            provider=TEAM_CREDITS_PROVIDER,
            status="active",
            currency="USD",
            current_period_start=now,
            current_period_end=period_end,
            team_id=team.id,
            seat_price_snapshot=price,
        )
        db.add(seat)
        await db.flush()
        _log(db, seat, "activated", {"team_id": str(team.id), "prorated_charge": charge})
        return seat

    @staticmethod
    async def change_tier(
        db: AsyncSession, team: Team, user_id: uuid.UUID, new_tier: str, now: datetime | None = None
    ) -> PlanSubscription:
        from src.services.teams import TeamService

        now = now or datetime.now()
        if team.status != "active":
            raise ValueError("Team is suspended")
        if new_tier not in PAID_TIERS:
            raise ValueError(f"Unknown or non-paid tier: {new_tier}")
        seat = await _active_seat(db, user_id)
        if seat is None or seat.team_id != team.id:
            raise ValueError("Member has no active seat")

        if is_upgrade(seat.tier, new_tier):
            # Immediate: prorated difference against the snapshot base for this month.
            new_price = TeamService.seat_price(team, new_tier)
            old_price = seat.seat_price_snapshot or TeamService.seat_price(team, seat.tier)
            charge = max(0.0, prorated_price(new_price - old_price, now))
            if charge > 0 and not await TeamCreditService.use_credits(db, team.id, charge):
                raise ValueError("Insufficient team balance — top up first")
            if charge > 0:
                await TeamCreditService.log(
                    db, team.id, "seat_charge_prorated", charge,
                    {"user_id": str(user_id), "tier": new_tier, "upgrade_from": seat.tier},
                )
            _log(db, seat, "upgraded", {"from": seat.tier, "to": new_tier, "prorated_charge": charge})
            seat.tier = new_tier
            seat.pending_tier = None
            seat.seat_price_snapshot = new_price
        elif is_downgrade(seat.tier, new_tier):
            seat.pending_tier = new_tier
            _log(db, seat, "downgrade_requested", {"new_tier": new_tier})
        else:
            raise ValueError("Seat is already on this tier")
        await db.flush()
        return seat

    @staticmethod
    async def cancel_seat(db: AsyncSession, team: Team, user_id: uuid.UUID) -> None:
        seat = await _active_seat(db, user_id)
        if seat is None or seat.team_id != team.id:
            raise ValueError("Member has no active seat")
        seat.cancel_at_period_end = True
        seat.pending_tier = DEFAULT_TIER
        _log(db, seat, "cancel_requested")
        await db.flush()

    @staticmethod
    async def suspend_team(db: AsyncSession, team: Team) -> None:
        """Suspension is mechanical: seats are expired NOW, so entitlement (which
        reads active subs only) stops granting the tier without any team join."""
        team.status = "suspended"
        seats = (
            await db.execute(
                select(PlanSubscription)
                .where(
                    PlanSubscription.team_id == team.id,
                    PlanSubscription.provider == TEAM_CREDITS_PROVIDER,
                    PlanSubscription.status == "active",
                )
                .with_for_update()
            )
        ).scalars().all()
        for seat in seats:
            seat.status = "expired"
            _log(db, seat, "team_suspended")
        await db.flush()

    @staticmethod
    async def process_renewals(
        db: AsyncSession, now: datetime | None = None
    ) -> tuple[int, list[dict]]:
        """Renew every team's due seats with ONE aggregate debit per team.

        All-or-nothing per team: on shortfall ALL renewing seats expire (no
        partial/priority charging). Returns (processed count, lapse notices) —
        the caller commits, then emails the notices (never inside the txn).
        """
        from src.services.teams import TeamService

        now = now or datetime.now()
        due = (
            await db.execute(
                select(PlanSubscription)
                .where(
                    PlanSubscription.provider == TEAM_CREDITS_PROVIDER,
                    PlanSubscription.status == "active",
                    PlanSubscription.current_period_end <= now,
                )
                .order_by(PlanSubscription.team_id)
                .with_for_update()
            )
        ).scalars().all()

        by_team: dict[uuid.UUID, list[PlanSubscription]] = {}
        for seat in due:
            if seat.team_id is None:  # defensive: team_credits seats always set this on creation
                continue
            by_team.setdefault(seat.team_id, []).append(seat)

        count = 0
        notices: list[dict] = []
        for team_id, seats in by_team.items():
            # One bad team must not block the whole batch (same idiom as
            # CreditSubscriptionService.process_renewals).
            try:
                async with db.begin_nested():
                    count += len(seats)
                    team = (
                        await db.execute(select(Team).where(Team.id == team_id).with_for_update())
                    ).scalar_one()

                    renewing = []
                    for seat in seats:
                        if (
                            team.status != "active"
                            or seat.cancel_at_period_end
                            or seat.pending_tier == DEFAULT_TIER
                        ):
                            seat.status = "expired"
                            _log(db, seat, "expired")
                        else:
                            renewing.append(seat)
                    if not renewing:
                        continue

                    total = round(
                        sum(TeamService.seat_price(team, s.pending_tier or s.tier) for s in renewing), 2
                    )
                    if total > 0 and not await TeamCreditService.use_credits(db, team.id, total):
                        for seat in renewing:
                            seat.status = "expired"
                            _log(db, seat, "expired_insufficient_team_balance")
                        notices.append(await _lapse_notice(db, team, total))
                        continue

                    if total > 0:
                        await TeamCreditService.log(
                            db, team.id, "monthly_renewal", total, {"seats": len(renewing)}
                        )
                    period_start, period_end = month_bounds(now)
                    for seat in renewing:
                        target = seat.pending_tier or seat.tier
                        # Anchor at the old period end (the 1st); if the cron was down
                        # for over a cycle, re-anchor at the current month instead of
                        # back-billing elapsed months.
                        start = seat.current_period_end or period_start
                        if start < period_start:
                            start = period_start
                        seat.tier = target
                        seat.pending_tier = None
                        seat.seat_price_snapshot = TeamService.seat_price(team, target)
                        seat.current_period_start = start
                        _, seat.current_period_end = month_bounds(start)
                        _log(db, seat, "renewed")
            except Exception:
                logger.error(f"Failed to process seat renewals for team {team_id}", exc_info=True)

        await db.flush()
        return count, notices


async def _lapse_notice(db: AsyncSession, team: Team, total: float) -> dict:
    admin_emails = (
        await db.execute(
            select(User.email)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .where(
                TeamMembership.team_id == team.id,
                TeamMembership.role == ROLE_ADMIN,
                User.email.isnot(None),
            )
        )
    ).scalars().all()
    return {
        "team_id": team.id,
        "team_name": team.name,
        "admin_emails": list(admin_emails),
        "total": total,
    }


async def send_lapse_emails(notices: list[dict]) -> None:
    for notice in notices:
        await send_email(
            notice["admin_emails"],
            f"[LibertAI] {notice['team_name']}: seat renewal failed",
            (
                f"<h2>Seat renewal failed for {notice['team_name']}</h2>"
                f"<p>The monthly renewal (${notice['total']:g}) could not be charged to your team "
                "balance, so all seats have been deactivated and members fell back to the free tier.</p>"
                "<p>Top up your team balance in the console, then re-assign the seats.</p>"
            ),
        )
