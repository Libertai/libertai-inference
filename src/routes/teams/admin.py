"""Staff-only team endpoints (``x-admin-token`` auth): provision teams, edit
pricing/caps, suspend, and mint the first admin invite."""

import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select

from src.interfaces.teams import (
    InviteRequest,
    InviteResponse,
    TeamCreateRequest,
    TeamResponse,
    TeamUpdateRequest,
)
from src.models.base import AsyncSessionLocal
from src.models.team import Team
from src.routes.teams import router
from src.routes.teams.teams import _invite_response, _team_response, send_invite_email
from src.services.auth import verify_admin_token
from src.services.payments.team_seat_subscription import TeamSeatService
from src.services.teams import TeamService


@router.post(
    "/admin", description="[staff] Create a team with negotiated pricing", dependencies=[Depends(verify_admin_token)]
)  # type: ignore
async def staff_create_team(body: TeamCreateRequest) -> TeamResponse:
    async with AsyncSessionLocal() as db:
        try:
            team = await TeamService.create_team(
                db,
                body.name,
                body.seat_prices,
                body.extra_credits_monthly_cap,
                body.extra_credits_member_default_cap,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return _team_response(team)


@router.patch(
    "/admin/{team_id}", description="[staff] Update a team's name/pricing/caps", dependencies=[Depends(verify_admin_token)]
)  # type: ignore
async def staff_update_team(team_id: uuid.UUID, body: TeamUpdateRequest) -> TeamResponse:
    async with AsyncSessionLocal() as db:
        team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
        if team is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
        if body.name is not None:
            team.name = body.name
        if body.seat_prices is not None:
            try:
                TeamService._validate_seat_prices(body.seat_prices)
            except ValueError as e:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
            team.seat_prices = body.seat_prices
        if body.extra_credits_monthly_cap is not None:
            team.extra_credits_monthly_cap = body.extra_credits_monthly_cap
        if body.extra_credits_member_default_cap is not None:
            team.extra_credits_member_default_cap = body.extra_credits_member_default_cap
        await db.flush()
        await db.commit()
        return _team_response(team)


@router.post(
    "/admin/{team_id}/suspend", description="[staff] Suspend a team (expires all seats)", dependencies=[Depends(verify_admin_token)]
)  # type: ignore
async def staff_suspend_team(team_id: uuid.UUID) -> TeamResponse:
    async with AsyncSessionLocal() as db:
        team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
        if team is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
        await TeamSeatService.suspend_team(db, team)
        await db.commit()
        return _team_response(team)


@router.post(
    "/admin/{team_id}/invites", description="[staff] Create the first admin invite", dependencies=[Depends(verify_admin_token)]
)  # type: ignore
async def staff_create_invite(team_id: uuid.UUID, body: InviteRequest) -> InviteResponse:
    async with AsyncSessionLocal() as db:
        team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
        if team is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
        try:
            invite, token = await TeamService.create_invite(db, team_id, body.email, body.role, "staff")
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
    await send_invite_email(team, invite.email, token)
    return _invite_response(invite)


@router.delete(
    "/admin/{team_id}/members/{user_id}",
    description="[staff] Remove a member (last-admin rule applies)",
    dependencies=[Depends(verify_admin_token)],
)  # type: ignore
async def staff_remove_member(team_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    async with AsyncSessionLocal() as db:
        team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
        if team is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
        try:
            await TeamService.remove_member(db, team_id, user_id, removed_by="staff")
        except (ValueError, PermissionError) as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        await db.commit()
        return {"status": "removed"}
