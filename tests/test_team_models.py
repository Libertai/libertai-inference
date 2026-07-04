"""Schema smoke tests for the Teams tables (created by conftest from metadata)."""

import uuid

import pytest
from sqlalchemy import select

from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.models.team import Team
from src.models.team_membership import ROLE_ADMIN, TeamMembership
from src.models.user import User


@pytest.mark.asyncio
async def test_team_membership_roundtrip(db):
    user = User(email=f"{uuid.uuid4()}@test.dev")
    team = Team(name="Acme")
    db.add_all([user, team])
    await db.flush()

    db.add(TeamMembership(team_id=team.id, user_id=user.id, role=ROLE_ADMIN))
    await db.flush()

    loaded = (await db.execute(select(TeamMembership).where(TeamMembership.user_id == user.id))).scalar_one()
    assert loaded.team_id == team.id
    assert loaded.role == ROLE_ADMIN
    assert team.status == "active"
    assert team.seat_prices == {}


@pytest.mark.asyncio
async def test_one_team_per_user_unique(db):
    user = User(email=f"{uuid.uuid4()}@test.dev")
    t1, t2 = Team(name="A"), Team(name="B")
    db.add_all([user, t1, t2])
    await db.flush()
    db.add(TeamMembership(team_id=t1.id, user_id=user.id))
    await db.flush()
    db.add(TeamMembership(team_id=t2.id, user_id=user.id))
    with pytest.raises(Exception):  # IntegrityError, wrapped by driver
        await db.flush()


@pytest.mark.asyncio
async def test_seat_columns_on_plan_subscription(db):
    user = User(email=f"{uuid.uuid4()}@test.dev")
    team = Team(name="Acme")
    db.add_all([user, team])
    await db.flush()
    sub = PlanSubscription(
        user_id=user.id, tier="plus", provider="team_credits",
        status="active", team_id=team.id, seat_price_snapshot=16.0,
    )
    db.add(sub)
    await db.flush()
    assert sub.team_id == team.id and sub.seat_price_snapshot == 16.0


@pytest.mark.asyncio
async def test_inference_call_team_id(db):
    # team_id is nullable and defaults to None; settable via kwarg.
    call = InferenceCall(api_key_id=uuid.uuid4(), credits_used=1.0, model_name="m", team_id=None)
    assert call.team_id is None
