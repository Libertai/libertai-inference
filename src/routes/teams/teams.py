"""Team-facing HTTP surface (members + team admins) and the seat-renewal cron.

Object-level authz: every ``/teams/{team_id}/*`` handler resolves the caller's
membership in THAT team via ``_load_team_as`` (admin flag when the action is
admin-only), so a caller can never read or mutate a team they don't belong to.
"""

import uuid
from datetime import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.payments import CheckoutResponse
from src.interfaces.teams import (
    AcceptInviteRequest,
    CapsRequest,
    InviteRequest,
    InviteResponse,
    LedgerChargeResponse,
    LedgerResponse,
    LedgerTopupResponse,
    MemberCapRequest,
    MemberResponse,
    MemberUsageResponse,
    RoleRequest,
    SeatAssignRequest,
    SeatChangeRequest,
    TeamMeResponse,
    TeamResponse,
    TeamTopupRequest,
)
from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import PlanSubscription
from src.models.team import Team
from src.models.team_credit_transaction import TeamCreditTransaction
from src.models.team_ledger_entry import TeamLedgerEntry
from src.models.team_membership import ROLE_ADMIN, TeamMembership
from src.models.user import User
from src.routes.teams import router
from src.services.auth import get_current_user
from src.services.entitlement import (
    WINDOW_5H,
    WINDOW_WEEKLY,
    _month_start,
    _team_extra_spend,
    window_usage_by_users,
)
from src.services.payments.manager import PaymentManager
from src.services.payments.registry import payment_registry
from src.services.payments.team_seat_subscription import (
    TEAM_CREDITS_PROVIDER,
    TeamSeatService,
    send_lapse_emails,
)
from src.services.team_credit import TeamCreditService
from src.services.teams import TeamService
from src.utils.cron import scheduler
from src.utils.email import send_email
from src.utils.frontend import resolve_frontend_base
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


# ------------------------------------------------------------------ helpers


def _team_response(team: Team) -> TeamResponse:
    return TeamResponse(
        id=team.id,
        name=team.name,
        status=team.status,
        seat_prices=team.seat_prices,
        extra_credits_monthly_cap=team.extra_credits_monthly_cap,
        extra_credits_member_default_cap=team.extra_credits_member_default_cap,
    )


def _invite_response(invite) -> InviteResponse:
    return InviteResponse(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        status=invite.status,
        expires_at=invite.expires_at,
    )


def _checkout_redirect(redirect_base: str | None) -> str:
    """Post-checkout return URL on the app the admin paid from (chat vs console)."""
    return f"{resolve_frontend_base(redirect_base)}/payment/callback"


def _require_provider(provider_id: str):
    """Duplicated (intentionally) from the payments route rather than importing a
    private name; keeps the team surface decoupled from that module's internals."""
    try:
        provider = payment_registry.get(provider_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown provider: {provider_id}")
    if not provider.descriptor().enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Provider {provider_id} is not configured",
        )
    return provider


async def send_invite_email(team: Team, email: str, token: str) -> None:
    """Fire-and-forget invite mail. Building the link or sending must never break
    the request (mirrors ``send_magic_link_email``)."""
    try:
        link = f"{resolve_frontend_base(None)}/teams/invite?token={token}"
    except ValueError:
        logger.error(f"FRONTEND_URL is not configured; cannot send team-invite email to {email}")
        return
    await send_email(
        [email],
        f"You're invited to {team.name} on LibertAI",
        (
            f"<h2>You've been invited to join {team.name} on LibertAI</h2>"
            f'<p><a href="{link}">Accept your invitation</a> (link expires soon).</p>'
        ),
    )


async def _load_team_as(
    db: AsyncSession, team_id: uuid.UUID, user: User, admin: bool
) -> tuple[Team, TeamMembership]:
    """Object-level authz: the caller must be a member (admin if required) of THIS team."""
    try:
        membership = await TeamService.require_membership(db, team_id, user.id, admin=admin)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return team, membership


async def _list_members(db: AsyncSession, team_id: uuid.UUID) -> list[MemberResponse]:
    rows = (
        await db.execute(
            select(TeamMembership, User, PlanSubscription)
            .join(User, User.id == TeamMembership.user_id)
            .outerjoin(
                PlanSubscription,
                and_(
                    PlanSubscription.user_id == TeamMembership.user_id,
                    PlanSubscription.provider == TEAM_CREDITS_PROVIDER,
                    PlanSubscription.status == "active",
                ),
            )
            .where(TeamMembership.team_id == team_id)
        )
    ).all()
    return [
        MemberResponse(
            user_id=m.user_id,
            email=u.email,
            display_name=u.display_name,
            role=m.role,
            seat_tier=s.tier if s else None,
            seat_status=s.status if s else None,
            seat_period_end=s.current_period_end if s else None,
            extra_credits_cap_override=m.extra_credits_cap_override,
        )
        for m, u, s in rows
    ]


# ------------------------------------------------------------------ cron


@scheduler.scheduled_job("interval", hours=1)
async def renew_team_seats() -> int:
    """Monthly seat renewals (idempotent hourly sweep, like renew_credit_subscriptions).

    Lapse emails go out AFTER the commit — a mail must never sit inside the txn."""
    async with AsyncSessionLocal() as db:
        count, notices = await TeamSeatService.process_renewals(db)
        await db.commit()
    await send_lapse_emails(notices)
    return count


# ------------------------------------------------------------------ member endpoints


@router.get("/me", description="Caller's team (with balance + members for admins)")  # type: ignore
async def get_my_team(user: User = Depends(get_current_user)) -> TeamMeResponse:
    async with AsyncSessionLocal() as db:
        membership = await TeamService.get_membership(db, user.id)
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You are not a member of any team")
        team = (await db.execute(select(Team).where(Team.id == membership.team_id))).scalar_one()
        own_seat = (
            await db.execute(
                select(PlanSubscription).where(
                    PlanSubscription.user_id == user.id,
                    PlanSubscription.provider == TEAM_CREDITS_PROVIDER,
                    PlanSubscription.status == "active",
                )
            )
        ).scalar_one_or_none()
        resp = TeamMeResponse(
            team=_team_response(team),
            role=membership.role,
            own_seat_tier=own_seat.tier if own_seat else None,
            own_seat_period_end=own_seat.current_period_end if own_seat else None,
        )
        # Ledger/balance and the member roster are admin-only.
        if membership.role == ROLE_ADMIN:
            resp.balance = await TeamCreditService.get_balance(db, team.id)
            resp.members = await _list_members(db, team.id)
        return resp


@router.post("/invites/accept", description="Accept a team invite (email must match)")  # type: ignore
async def accept_invite(body: AcceptInviteRequest, user: User = Depends(get_current_user)) -> dict:
    async with AsyncSessionLocal() as db:
        try:
            membership = await TeamService.accept_invite(db, body.token, user)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"team_id": str(membership.team_id), "role": membership.role}


@router.post("/leave", description="Leave your team (last admin cannot leave)")  # type: ignore
async def leave_team(user: User = Depends(get_current_user)) -> dict:
    async with AsyncSessionLocal() as db:
        try:
            await TeamService.leave(db, user.id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"status": "left"}


# ------------------------------------------------------------------ team-admin endpoints


@router.post("/{team_id}/invites", description="[admin] Invite a member by email")  # type: ignore
async def create_team_invite(
    team_id: uuid.UUID, body: InviteRequest, user: User = Depends(get_current_user)
) -> InviteResponse:
    async with AsyncSessionLocal() as db:
        team, _ = await _load_team_as(db, team_id, user, admin=True)
        try:
            invite, token = await TeamService.create_invite(db, team_id, body.email, body.role, invited_by=user.id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    await send_invite_email(team, invite.email, token)
    return _invite_response(invite)


@router.delete("/{team_id}/invites/{invite_id}", description="[admin] Revoke a pending invite")  # type: ignore
async def revoke_team_invite(
    team_id: uuid.UUID, invite_id: uuid.UUID, user: User = Depends(get_current_user)
) -> dict:
    async with AsyncSessionLocal() as db:
        await _load_team_as(db, team_id, user, admin=True)
        try:
            await TeamService.revoke_invite(db, team_id, invite_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"status": "revoked"}


@router.delete("/{team_id}/members/{user_id}", description="[admin] Remove a member")  # type: ignore
async def remove_team_member(
    team_id: uuid.UUID, user_id: uuid.UUID, user: User = Depends(get_current_user)
) -> dict:
    async with AsyncSessionLocal() as db:
        await _load_team_as(db, team_id, user, admin=True)
        try:
            await TeamService.remove_member(db, team_id, user_id, removed_by=user.id)
        except (ValueError, PermissionError) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"status": "removed"}


@router.post("/{team_id}/members/{user_id}/role", description="[admin] Set a member's role")  # type: ignore
async def set_member_role(
    team_id: uuid.UUID, user_id: uuid.UUID, body: RoleRequest, user: User = Depends(get_current_user)
) -> dict:
    async with AsyncSessionLocal() as db:
        await _load_team_as(db, team_id, user, admin=True)
        try:
            await TeamService.set_role(db, team_id, user.id, user_id, body.role)
        except (ValueError, PermissionError) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"status": "ok", "role": body.role}


@router.post("/{team_id}/seats", description="[admin] Assign a seat to a member")  # type: ignore
async def assign_seat(
    team_id: uuid.UUID, body: SeatAssignRequest, user: User = Depends(get_current_user)
) -> dict:
    async with AsyncSessionLocal() as db:
        team, _ = await _load_team_as(db, team_id, user, admin=True)
        try:
            seat = await TeamSeatService.assign_seat(db, team, body.user_id, body.tier)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"seat_id": str(seat.id), "tier": seat.tier}


@router.patch("/{team_id}/seats/{user_id}", description="[admin] Change a member's seat tier")  # type: ignore
async def change_seat(
    team_id: uuid.UUID, user_id: uuid.UUID, body: SeatChangeRequest, user: User = Depends(get_current_user)
) -> dict:
    async with AsyncSessionLocal() as db:
        team, _ = await _load_team_as(db, team_id, user, admin=True)
        try:
            seat = await TeamSeatService.change_tier(db, team, user_id, body.tier)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"seat_id": str(seat.id), "tier": seat.tier, "pending_tier": seat.pending_tier}


@router.delete("/{team_id}/seats/{user_id}", description="[admin] Cancel a member's seat at period end")  # type: ignore
async def cancel_seat(
    team_id: uuid.UUID, user_id: uuid.UUID, user: User = Depends(get_current_user)
) -> dict:
    async with AsyncSessionLocal() as db:
        team, _ = await _load_team_as(db, team_id, user, admin=True)
        try:
            await TeamSeatService.cancel_seat(db, team, user_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"status": "cancel_scheduled"}


@router.patch("/{team_id}/caps", description="[admin] Set the team's extra-credits caps")  # type: ignore
async def set_team_caps(
    team_id: uuid.UUID, body: CapsRequest, user: User = Depends(get_current_user)
) -> TeamResponse:
    async with AsyncSessionLocal() as db:
        team, _ = await _load_team_as(db, team_id, user, admin=True)
        team.extra_credits_monthly_cap = body.extra_credits_monthly_cap
        team.extra_credits_member_default_cap = body.extra_credits_member_default_cap
        await db.flush()
        await db.commit()
        return _team_response(team)


@router.patch("/{team_id}/members/{user_id}/cap", description="[admin] Override a member's extra-credits cap")  # type: ignore
async def set_member_cap(
    team_id: uuid.UUID, user_id: uuid.UUID, body: MemberCapRequest, user: User = Depends(get_current_user)
) -> MemberResponse:
    async with AsyncSessionLocal() as db:
        await _load_team_as(db, team_id, user, admin=True)
        target = (
            await db.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
        target.extra_credits_cap_override = body.extra_credits_cap_override
        await db.flush()
        target_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        seat = (
            await db.execute(
                select(PlanSubscription).where(
                    PlanSubscription.user_id == user_id,
                    PlanSubscription.provider == TEAM_CREDITS_PROVIDER,
                    PlanSubscription.status == "active",
                )
            )
        ).scalar_one_or_none()
        await db.commit()
        return MemberResponse(
            user_id=target.user_id,
            email=target_user.email,
            display_name=target_user.display_name,
            role=target.role,
            seat_tier=seat.tier if seat else None,
            seat_status=seat.status if seat else None,
            seat_period_end=seat.current_period_end if seat else None,
            extra_credits_cap_override=target.extra_credits_cap_override,
        )


@router.get("/{team_id}/ledger", description="[admin] Team balance + recent top-ups and charges")  # type: ignore
async def get_ledger(team_id: uuid.UUID, user: User = Depends(get_current_user)) -> LedgerResponse:
    async with AsyncSessionLocal() as db:
        await _load_team_as(db, team_id, user, admin=True)
        balance = await TeamCreditService.get_balance(db, team_id)
        topups = (
            await db.execute(
                select(TeamCreditTransaction)
                .where(TeamCreditTransaction.team_id == team_id)
                .order_by(TeamCreditTransaction.created_at.desc())
                .limit(100)
            )
        ).scalars().all()
        charges = (
            await db.execute(
                select(TeamLedgerEntry)
                .where(TeamLedgerEntry.team_id == team_id)
                .order_by(TeamLedgerEntry.created_at.desc())
                .limit(100)
            )
        ).scalars().all()
        return LedgerResponse(
            balance=balance,
            topups=[
                LedgerTopupResponse(amount=t.amount, status=t.status.value, created_at=t.created_at)
                for t in topups
            ],
            charges=[
                LedgerChargeResponse(
                    entry_type=c.entry_type, amount=c.amount, metadata=c.metadata_json, created_at=c.created_at
                )
                for c in charges
            ],
        )


@router.get("/{team_id}/usage", description="[admin] Per-member window usage + extra-credits spend")  # type: ignore
async def get_usage(team_id: uuid.UUID, user: User = Depends(get_current_user)) -> list[MemberUsageResponse]:
    now = datetime.now()
    month_start = _month_start(now)
    async with AsyncSessionLocal() as db:
        await _load_team_as(db, team_id, user, admin=True)
        members = (
            await db.execute(
                select(TeamMembership, User)
                .join(User, User.id == TeamMembership.user_id)
                .where(TeamMembership.team_id == team_id)
            )
        ).all()
        user_ids = {m.user_id for m, _ in members}
        seats = {
            s.user_id: s
            for s in (
                await db.execute(
                    select(PlanSubscription).where(
                        PlanSubscription.team_id == team_id,
                        PlanSubscription.provider == TEAM_CREDITS_PROVIDER,
                        PlanSubscription.status == "active",
                    )
                )
            ).scalars().all()
        }
        used_5h = await window_usage_by_users(db, user_ids, WINDOW_5H, now)
        used_weekly = await window_usage_by_users(db, user_ids, WINDOW_WEEKLY, now)
        return [
            MemberUsageResponse(
                user_id=m.user_id,
                email=u.email,
                seat_tier=seats[m.user_id].tier if m.user_id in seats else None,
                window_5h_used=used_5h.get(m.user_id, 0.0),
                weekly_used=used_weekly.get(m.user_id, 0.0),
                extra_credits_month_to_date=await _team_extra_spend(
                    db, team_id, month_start, user_id=m.user_id
                ),
            )
            for m, u in members
        ]


@router.post("/{team_id}/topup", description="[admin] Open a checkout to fund the team balance")  # type: ignore
async def team_topup(
    team_id: uuid.UUID, body: TeamTopupRequest, user: User = Depends(get_current_user)
) -> CheckoutResponse:
    async with AsyncSessionLocal() as db:
        team, _ = await _load_team_as(db, team_id, user, admin=True)
        provider = _require_provider("revolut")
        try:
            result = await PaymentManager(provider, db).start_team_topup(
                team,
                user.email,
                _checkout_redirect(body.redirect_base),
                usd_credits=body.amount,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    return CheckoutResponse(checkout_url=result.checkout_url)
