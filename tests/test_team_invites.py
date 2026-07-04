import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from src.interfaces.credits import CreditTransactionProvider
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.team_invite import TeamInvite
from src.models.team_membership import ROLE_ADMIN, ROLE_MEMBER
from src.models.user import User
from src.services.teams import TeamService


async def _team_and_user(db, email=None):
    team = await TeamService.create_team(db, "Acme", seat_prices={"plus": 16.0})
    user = User(email=email or f"{uuid.uuid4()}@test.dev", email_verified=True)
    db.add(user)
    await db.flush()
    return team, user


@pytest.mark.asyncio
async def test_invite_token_hashed_and_accept(db):
    team, user = await _team_and_user(db)
    invite, token = await TeamService.create_invite(db, team.id, user.email.upper(), ROLE_MEMBER, "staff")
    assert invite.token_hash != token  # never stored in plaintext
    assert invite.email == user.email.lower()

    membership = await TeamService.accept_invite(db, token, user)
    assert membership.team_id == team.id and membership.role == ROLE_MEMBER
    assert invite.status == "accepted"
    # Single-use: second acceptance fails.
    other = User(email=f"{uuid.uuid4()}@test.dev")
    db.add(other)
    await db.flush()
    with pytest.raises(ValueError, match="Invalid or expired"):
        await TeamService.accept_invite(db, token, other)


@pytest.mark.asyncio
async def test_accept_requires_email_match(db):
    team, user = await _team_and_user(db)
    _, token = await TeamService.create_invite(db, team.id, "someone.else@corp.dev", ROLE_MEMBER, "staff")
    with pytest.raises(ValueError, match="different email"):
        await TeamService.accept_invite(db, token, user)


@pytest.mark.asyncio
async def test_accept_blocked_by_active_sub_and_balance(db):
    team, user = await _team_and_user(db)
    _, token = await TeamService.create_invite(db, team.id, user.email, ROLE_MEMBER, "staff")

    sub = PlanSubscription(user_id=user.id, tier="plus", provider="revolut", status="active")
    db.add(sub)
    await db.flush()
    with pytest.raises(ValueError, match="subscription"):
        await TeamService.accept_invite(db, token, user)

    sub.status = "expired"
    db.add(CreditTransaction(user_id=user.id, amount=5.0, amount_left=5.0,
                             provider=CreditTransactionProvider.voucher))
    await db.flush()
    with pytest.raises(ValueError, match="balance"):
        await TeamService.accept_invite(db, token, user)


@pytest.mark.asyncio
async def test_accept_blocked_if_already_in_a_team(db):
    team, user = await _team_and_user(db)
    _, token = await TeamService.create_invite(db, team.id, user.email, ROLE_ADMIN, "staff")
    await TeamService.accept_invite(db, token, user)

    team2 = await TeamService.create_team(db, "Other")
    _, token2 = await TeamService.create_invite(db, team2.id, user.email, ROLE_MEMBER, "staff")
    with pytest.raises(ValueError, match="already"):
        await TeamService.accept_invite(db, token2, user)


@pytest.mark.asyncio
async def test_accept_duplicate_membership_race_raises_value_error(db, monkeypatch):
    """If the get_membership check is raced past, the unique constraint surfaces as
    ValueError (not a raw IntegrityError 500)."""
    from src.models.team_membership import TeamMembership

    team, user = await _team_and_user(db)
    _, token = await TeamService.create_invite(db, team.id, user.email, ROLE_MEMBER, "staff")
    # Pre-existing membership simulates a concurrent accept that already committed.
    db.add(TeamMembership(team_id=team.id, user_id=user.id, role=ROLE_MEMBER))
    await db.flush()
    # Make the pre-insert guard miss it, forcing the flush to trip the unique constraint.
    monkeypatch.setattr(TeamService, "get_membership", staticmethod(lambda db, user_id: _none()))
    with pytest.raises(ValueError, match="already in a team"):
        await TeamService.accept_invite(db, token, user)


async def _none():
    return None


@pytest.mark.asyncio
async def test_expired_invite_rejected(db):
    team, user = await _team_and_user(db)
    invite, token = await TeamService.create_invite(db, team.id, user.email, ROLE_MEMBER, "staff")
    invite.expires_at = datetime.now() - timedelta(days=1)
    await db.flush()
    with pytest.raises(ValueError, match="Invalid or expired"):
        await TeamService.accept_invite(db, token, user)


@pytest.mark.asyncio
async def test_new_invite_revokes_older_pending_for_same_email(db):
    team, user = await _team_and_user(db)
    old, _ = await TeamService.create_invite(db, team.id, user.email, ROLE_MEMBER, "staff")
    await TeamService.create_invite(db, team.id, user.email, ROLE_MEMBER, "staff")
    refreshed = (await db.execute(select(TeamInvite).where(TeamInvite.id == old.id))).scalar_one()
    assert refreshed.status == "revoked"


@pytest.mark.asyncio
async def test_accept_requires_verified_email(db):
    team = await TeamService.create_team(db, "Acme", seat_prices={"plus": 16.0})
    email = f"{uuid.uuid4()}@test.dev"
    user = User(email=email, email_verified=False)
    db.add(user)
    await db.flush()
    _, token = await TeamService.create_invite(db, team.id, email, ROLE_MEMBER, "staff")
    with pytest.raises(ValueError, match="Verify your email"):
        await TeamService.accept_invite(db, token, user)
