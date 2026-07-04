import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.team_membership import ROLE_ADMIN, ROLE_MEMBER, TeamMembership
from src.models.user import User
from src.services.payments.team_seat_subscription import TEAM_CREDITS_PROVIDER
from src.services.teams import TeamService


async def _setup(db, n_members=2):
    team = await TeamService.create_team(db, "Acme", seat_prices={"plus": 16.0, "max": 80.0})
    users = []
    for i in range(n_members):
        u = User(email=f"{uuid.uuid4()}@test.dev")
        db.add(u)
        await db.flush()
        db.add(TeamMembership(team_id=team.id, user_id=u.id, role=ROLE_ADMIN if i == 0 else ROLE_MEMBER))
        users.append(u)
    await db.flush()
    return team, users


@pytest.mark.asyncio
async def test_create_team_validates_seat_prices(db):
    with pytest.raises(ValueError, match="Unknown tier"):
        await TeamService.create_team(db, "Bad", seat_prices={"platinum": 50.0})
    with pytest.raises(ValueError, match="positive"):
        await TeamService.create_team(db, "Bad", seat_prices={"plus": 0})


@pytest.mark.asyncio
async def test_seat_price_negotiated_vs_list_vs_unsellable(db):
    team, _ = await _setup(db)
    assert TeamService.seat_price(team, "plus") == 16.0
    with pytest.raises(ValueError, match="not sold"):
        TeamService.seat_price(team, "go")  # non-empty map without "go"
    empty = await TeamService.create_team(db, "ListPrices")
    assert TeamService.seat_price(empty, "plus") == 20.0  # list price


@pytest.mark.asyncio
async def test_require_membership_and_admin(db):
    team, (admin, member) = await _setup(db)
    outsider = User(email=f"{uuid.uuid4()}@test.dev")
    db.add(outsider)
    await db.flush()
    assert (await TeamService.require_membership(db, team.id, admin.id, admin=True)).role == ROLE_ADMIN
    with pytest.raises(PermissionError):
        await TeamService.require_membership(db, team.id, member.id, admin=True)
    with pytest.raises(PermissionError):
        await TeamService.require_membership(db, team.id, outsider.id)


@pytest.mark.asyncio
async def test_last_admin_cannot_demote_or_leave(db):
    team, (admin, member) = await _setup(db)
    with pytest.raises(ValueError, match="last admin"):
        await TeamService.set_role(db, team.id, admin.id, admin.id, ROLE_MEMBER)
    with pytest.raises(ValueError, match="last admin"):
        await TeamService.leave(db, admin.id)
    # Promote the member, then the original admin may demote/leave.
    await TeamService.set_role(db, team.id, admin.id, member.id, ROLE_ADMIN)
    await TeamService.leave(db, admin.id)
    assert await TeamService.get_membership(db, admin.id) is None


@pytest.mark.asyncio
async def test_remove_member_expires_seat_and_logs_actor(db):
    team, (admin, member) = await _setup(db)
    seat = PlanSubscription(
        user_id=member.id, tier="plus", provider=TEAM_CREDITS_PROVIDER, status="active",
        team_id=team.id, seat_price_snapshot=16.0,
        current_period_start=datetime.now(), current_period_end=datetime.now() + timedelta(days=10),
    )
    db.add(seat)
    await db.flush()

    await TeamService.remove_member(db, team.id, member.id, removed_by=admin.id)

    assert await TeamService.get_membership(db, member.id) is None
    assert seat.status == "expired"
    event = (
        await db.execute(
            select(PlanSubscriptionEvent).where(PlanSubscriptionEvent.subscription_id == seat.id)
        )
    ).scalars().first()
    assert event.event_type == "member_removed"
    assert event.metadata_json["removed_by"] == str(admin.id)
