import uuid
from datetime import datetime, timedelta

import pytest

from src.models.plan_subscription import PlanSubscription
from src.models.team import Team
from src.models.user import User
from src.services.payments.credit_subscription import CreditSubscriptionService
from src.services.payments.manager import PaymentManager
from src.services.payments.team_seat_subscription import TEAM_CREDITS_PROVIDER


async def _user_with_seat(db, period_end=None) -> tuple[User, Team, PlanSubscription]:
    user = User(email=f"{uuid.uuid4()}@test.dev")
    team = Team(name="Acme")
    db.add_all([user, team])
    await db.flush()
    seat = PlanSubscription(
        user_id=user.id, tier="plus", provider=TEAM_CREDITS_PROVIDER, status="active",
        team_id=team.id, seat_price_snapshot=16.0,
        current_period_start=datetime.now() - timedelta(days=40),
        current_period_end=period_end or (datetime.now() - timedelta(days=10)),
        cancel_at_period_end=True,
    )
    db.add(seat)
    await db.flush()
    return user, team, seat


class _NoProvider:
    """PaymentManager provider stub — must never be called for seats."""
    id = "revolut"

    def __getattr__(self, name):  # any provider call is a test failure
        raise AssertionError(f"provider.{name} must not be called for team seats")


@pytest.mark.asyncio
async def test_active_subscription_ignores_seats(db):
    user, _, _ = await _user_with_seat(db)
    manager = PaymentManager(_NoProvider(), db)
    assert await manager._active_subscription(user.id) is None


@pytest.mark.asyncio
async def test_manager_cancel_rejects_seat_holder(db):
    user, _, seat = await _user_with_seat(db)
    manager = PaymentManager(_NoProvider(), db)
    with pytest.raises(ValueError):
        await manager.cancel(user)
    assert seat.status == "active"  # untouched


@pytest.mark.asyncio
async def test_check_expirations_leaves_seats_alone(db):
    # Seat past period end with cancel_at_period_end=True — exactly what the cron
    # expires for personal subs. Team seats are owned by the team cron instead.
    _, _, seat = await _user_with_seat(db)
    manager = PaymentManager(_NoProvider(), db)
    await manager.check_expirations()
    assert seat.status == "active"


@pytest.mark.asyncio
async def test_process_renewals_ignores_seats(db):
    _, _, seat = await _user_with_seat(db)
    await CreditSubscriptionService.process_renewals(db)
    assert seat.status == "active"  # provider filter already excludes team_credits


@pytest.mark.asyncio
async def test_credit_subscribe_blocked_by_seat(db):
    # The one-live-sub guard treats a seat as the existing subscription.
    user, _, _ = await _user_with_seat(db, period_end=datetime.now() + timedelta(days=10))
    with pytest.raises(ValueError, match="active subscription"):
        await CreditSubscriptionService.subscribe(db, user, "plus")
