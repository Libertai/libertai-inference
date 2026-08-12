"""Cutover script: revives the paid row parked in ``upgrading``, retires unpaid checkouts."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from scripts.cutover_pending_upgrade import cutover
from tests.test_payment_manager import FakeProvider


async def _make_user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_promotes_the_paid_parked_row_and_expires_the_rest(db):
    user = await _make_user(db)
    paid = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="upgrading",
        provider_subscription_id="psub_paid",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    )
    unpaid_parked = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="upgrading",
        provider_subscription_id="psub_parked",
    )
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending",
        provider_subscription_id="psub_checkout",
    )
    db.add_all([paid, unpaid_parked, checkout])
    await db.flush()

    counts = await cutover(db, FakeProvider())

    await db.refresh(paid)
    await db.refresh(unpaid_parked)
    await db.refresh(checkout)
    assert paid.status == "active"
    assert unpaid_parked.status == "expired"
    assert checkout.status == "expired"
    assert counts["promoted"] == 1


@pytest.mark.asyncio
async def test_expired_rows_carry_the_abandoned_checkout_event(db):
    """Plain `expired` would register as churn and disarm the activation refusal."""
    user = await _make_user(db)
    stale = PlanSubscription(
        user_id=user.id, tier="go", provider="fake", status="pending",
        provider_subscription_id="psub_stale",
    )
    db.add(stale)
    await db.flush()

    await cutover(db, FakeProvider())

    events = (await db.execute(
        select(PlanSubscriptionEvent.event_type).where(PlanSubscriptionEvent.subscription_id == stale.id)
    )).scalars().all()
    assert events == ["expired_abandoned_checkout"]


@pytest.mark.asyncio
async def test_user_with_a_live_row_is_not_promoted(db):
    user = await _make_user(db)
    live = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="active",
        provider_subscription_id="psub_live",
        current_period_start=datetime.now() - timedelta(days=1),
        current_period_end=datetime.now() + timedelta(days=29),
    )
    parked = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="upgrading",
        provider_subscription_id="psub_parked",
        current_period_start=datetime.now() - timedelta(days=40),
        current_period_end=datetime.now() - timedelta(days=10),
    )
    db.add_all([live, parked])
    await db.flush()

    counts = await cutover(db, FakeProvider())

    await db.refresh(live)
    await db.refresh(parked)
    assert live.status == "active"
    assert parked.status == "cancelled"  # given an explicit disposition, never left stranded
    assert counts["stranded"] == 0
