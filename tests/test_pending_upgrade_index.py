import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from src.models.plan_subscription import ACTIVE_STATUSES, UNPAID_CHECKOUT_STATUSES, PlanSubscription
from src.models.user import User


async def _user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    await db.flush()
    return user


def test_status_constants():
    assert ACTIVE_STATUSES == ("pending", "active", "overdue")
    assert UNPAID_CHECKOUT_STATUSES == ("pending", "pending_upgrade")
    assert "pending_upgrade" not in ACTIVE_STATUSES


@pytest.mark.asyncio
async def test_pending_upgrade_coexists_with_active(db):
    """The whole mechanism: an upgrade checkout may sit alongside the sub it replaces."""
    user = await _user(db)
    db.add(PlanSubscription(user_id=user.id, tier="plus", provider="fake", status="active"))
    db.add(PlanSubscription(user_id=user.id, tier="max", provider="fake", status="pending_upgrade"))
    await db.flush()


@pytest.mark.asyncio
async def test_only_one_pending_upgrade_per_user(db):
    user = await _user(db)
    db.add(PlanSubscription(user_id=user.id, tier="max", provider="fake", status="pending_upgrade"))
    await db.flush()
    db.add(PlanSubscription(user_id=user.id, tier="max", provider="fake", status="pending_upgrade"))
    with pytest.raises(IntegrityError):
        await db.flush()
