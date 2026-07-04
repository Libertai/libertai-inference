import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from src.interfaces.credits import CreditTransactionProvider
from src.models.plan_subscription import PlanSubscription
from src.models.team_ledger_entry import TeamLedgerEntry
from src.models.team_membership import ROLE_ADMIN, ROLE_MEMBER, TeamMembership
from src.models.user import User
from src.services.payments.team_seat_subscription import (
    TEAM_CREDITS_PROVIDER,
    TeamSeatService,
    month_bounds,
    prorated_price,
)
from src.services.team_credit import TeamCreditService
from src.services.teams import TeamService


async def _team_with_member(db, balance=1000.0, seat_prices=None):
    team = await TeamService.create_team(db, "Acme", seat_prices=seat_prices or {"plus": 16.0, "max": 80.0})
    user = User(email=f"{uuid.uuid4()}@test.dev")
    admin = User(email=f"{uuid.uuid4()}@test.dev")
    db.add_all([user, admin])
    await db.flush()
    db.add_all([
        TeamMembership(team_id=team.id, user_id=user.id, role=ROLE_MEMBER),
        TeamMembership(team_id=team.id, user_id=admin.id, role=ROLE_ADMIN),
    ])
    await db.flush()
    if balance:
        await TeamCreditService.add_credits(db, team.id, balance, CreditTransactionProvider.revolut)
    return team, user, admin


def test_prorated_price_includes_today():
    # June has 30 days.
    assert prorated_price(30.0, datetime(2026, 6, 1, 12)) == 30.0     # 1st = full month
    assert prorated_price(30.0, datetime(2026, 6, 30, 12)) == 1.0     # last day = 1/30
    assert prorated_price(30.0, datetime(2026, 6, 16, 0)) == 15.0     # 15 days left incl. today


def test_month_bounds():
    start, end = month_bounds(datetime(2026, 12, 31, 23, 59))
    assert start == datetime(2026, 12, 1) and end == datetime(2027, 1, 1)


@pytest.mark.asyncio
async def test_assign_seat_charges_prorated_and_snapshots(db):
    team, user, _ = await _team_with_member(db)
    now = datetime(2026, 6, 16)  # 15/30 days left
    seat = await TeamSeatService.assign_seat(db, team, user.id, "plus", now=now)

    assert seat.provider == TEAM_CREDITS_PROVIDER and seat.status == "active"
    assert seat.team_id == team.id and seat.seat_price_snapshot == 16.0
    assert seat.current_period_end == datetime(2026, 7, 1)
    assert await TeamCreditService.get_balance(db, team.id) == 1000.0 - 8.0
    entry = (
        await db.execute(select(TeamLedgerEntry).where(TeamLedgerEntry.team_id == team.id))
    ).scalar_one()
    assert entry.entry_type == "seat_charge_prorated" and entry.amount == 8.0


@pytest.mark.asyncio
async def test_assign_seat_requires_membership_funds_and_active_team(db):
    team, user, _ = await _team_with_member(db, balance=1.0)
    with pytest.raises(ValueError, match="Insufficient team balance"):
        await TeamSeatService.assign_seat(db, team, user.id, "plus", now=datetime(2026, 6, 2))
    outsider = User(email=f"{uuid.uuid4()}@test.dev")
    db.add(outsider)
    await db.flush()
    with pytest.raises(ValueError, match="not a member"):
        await TeamSeatService.assign_seat(db, team, outsider.id, "plus")
    team.status = "suspended"
    with pytest.raises(ValueError, match="suspended"):
        await TeamSeatService.assign_seat(db, team, user.id, "plus")


@pytest.mark.asyncio
async def test_upgrade_charges_prorated_difference(db):
    team, user, _ = await _team_with_member(db)
    now = datetime(2026, 6, 16)
    await TeamSeatService.assign_seat(db, team, user.id, "plus", now=now)  # -8.0
    seat = await TeamSeatService.change_tier(db, team, user.id, "max", now=now)  # +(80-16)*0.5 = 32.0
    assert seat.tier == "max" and seat.seat_price_snapshot == 80.0
    assert await TeamCreditService.get_balance(db, team.id) == 1000.0 - 8.0 - 32.0


@pytest.mark.asyncio
async def test_downgrade_sets_pending_tier(db):
    team, user, _ = await _team_with_member(db)
    await TeamSeatService.assign_seat(db, team, user.id, "max", now=datetime(2026, 6, 2))
    seat = await TeamSeatService.change_tier(db, team, user.id, "plus", now=datetime(2026, 6, 10))
    assert seat.tier == "max" and seat.pending_tier == "plus"


@pytest.mark.asyncio
async def test_renewal_aggregate_debit_and_roll(db):
    team, user, admin = await _team_with_member(db)
    now = datetime(2026, 6, 16)
    await TeamSeatService.assign_seat(db, team, user.id, "plus", now=now)
    await TeamSeatService.assign_seat(db, team, admin.id, "max", now=now)
    balance_before = await TeamCreditService.get_balance(db, team.id)

    processed, notices = await TeamSeatService.process_renewals(db, now=datetime(2026, 7, 1, 0, 30))
    assert processed == 2 and notices == []
    assert await TeamCreditService.get_balance(db, team.id) == balance_before - 96.0  # 16 + 80
    seats = (
        await db.execute(select(PlanSubscription).where(PlanSubscription.team_id == team.id))
    ).scalars().all()
    for seat in seats:
        assert seat.status == "active"
        assert seat.current_period_start == datetime(2026, 7, 1)
        assert seat.current_period_end == datetime(2026, 8, 1)
    renewal = (
        await db.execute(
            select(TeamLedgerEntry).where(
                TeamLedgerEntry.team_id == team.id, TeamLedgerEntry.entry_type == "monthly_renewal"
            )
        )
    ).scalar_one()
    assert renewal.amount == 96.0  # single aggregate statement line


@pytest.mark.asyncio
async def test_renewal_honors_cancel_and_pending_tier(db):
    team, user, admin = await _team_with_member(db)
    now = datetime(2026, 6, 16)
    s1 = await TeamSeatService.assign_seat(db, team, user.id, "plus", now=now)
    s2 = await TeamSeatService.assign_seat(db, team, admin.id, "max", now=now)
    await TeamSeatService.cancel_seat(db, team, user.id)
    await TeamSeatService.change_tier(db, team, admin.id, "plus", now=now)  # downgrade pending

    balance_before = await TeamCreditService.get_balance(db, team.id)
    await TeamSeatService.process_renewals(db, now=datetime(2026, 7, 1, 0, 30))

    assert s1.status == "expired"
    assert s2.status == "active" and s2.tier == "plus" and s2.pending_tier is None
    assert s2.seat_price_snapshot == 16.0
    assert await TeamCreditService.get_balance(db, team.id) == balance_before - 16.0


@pytest.mark.asyncio
async def test_renewal_shortfall_expires_all_and_notifies(db):
    team, user, admin = await _team_with_member(db, balance=0.0)
    now = datetime(2026, 6, 16)
    await TeamCreditService.add_credits(db, team.id, 48.5, CreditTransactionProvider.revolut)
    await TeamSeatService.assign_seat(db, team, user.id, "plus", now=now)   # -8
    await TeamSeatService.assign_seat(db, team, admin.id, "max", now=now)   # -40  -> 0.5 left

    processed, notices = await TeamSeatService.process_renewals(db, now=datetime(2026, 7, 1, 0, 30))
    assert processed == 2
    seats = (
        await db.execute(select(PlanSubscription).where(PlanSubscription.team_id == team.id))
    ).scalars().all()
    assert all(s.status == "expired" for s in seats)
    assert await TeamCreditService.get_balance(db, team.id) == 0.5  # nothing partially charged
    assert len(notices) == 1
    assert notices[0]["team_id"] == team.id and admin.email in notices[0]["admin_emails"]


@pytest.mark.asyncio
async def test_renewal_reanchors_after_long_downtime(db):
    team, user, _ = await _team_with_member(db)
    seat = await TeamSeatService.assign_seat(db, team, user.id, "plus", now=datetime(2026, 6, 16))
    # Cron dead for > a full cycle: re-anchor at the current month, no back-billing.
    await TeamSeatService.process_renewals(db, now=datetime(2026, 9, 10))
    assert seat.current_period_start == datetime(2026, 9, 1)
    assert seat.current_period_end == datetime(2026, 10, 1)


@pytest.mark.asyncio
async def test_suspend_team_expires_seats(db):
    team, user, _ = await _team_with_member(db)
    seat = await TeamSeatService.assign_seat(db, team, user.id, "plus", now=datetime(2026, 6, 2))
    await TeamSeatService.suspend_team(db, team)
    assert team.status == "suspended" and seat.status == "expired"
