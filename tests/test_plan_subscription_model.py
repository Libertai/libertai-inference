"""plan_subscriptions product/owner constraints."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from src.models.plan_subscription import PlanSubscription


async def test_liberclaw_row_needs_no_user(db):
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        status="pending",
        provider="revolut",
        product="liberclaw",
        liberclaw_account_id=uuid.uuid4(),
    )
    db.add(sub)
    await db.flush()
    assert sub.product == "liberclaw" and sub.provider_cancelled is False


async def test_row_with_no_owner_rejected(db):
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        status="pending",
        provider="revolut",
        product="liberclaw",
        liberclaw_account_id=None,
    )
    db.add(sub)
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_one_live_sub_per_liberclaw_account(db):
    acc = uuid.uuid4()
    for status in ("active", "pending"):
        db.add(
            PlanSubscription(
                user_id=None,
                tier="starter",
                status=status,
                provider="revolut",
                product="liberclaw",
                liberclaw_account_id=acc,
            )
        )
    with pytest.raises(IntegrityError):
        await db.flush()
