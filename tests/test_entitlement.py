"""Dual-window entitlement service tests (freezegun for deterministic windows)."""

import uuid
from datetime import datetime, timedelta

import pytest

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.credit_transaction import CreditTransaction
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.services.entitlement import get_allowance_state

NOW = datetime(2026, 6, 2, 12, 0, 0)


async def _user(db) -> User:
    u = User(email=f"{uuid.uuid4().hex}@example.com", email_verified=True)
    db.add(u)
    await db.flush()
    return u


async def _api_key(db, user_id) -> ApiKeyDB:
    k = ApiKeyDB(key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, user_id=user_id, type=ApiKeyType.api)
    db.add(k)
    await db.flush()
    return k


async def _use(db, api_key_id, credits, when: datetime):
    call = InferenceCall(api_key_id=api_key_id, credits_used=credits, model_name="m")
    call.used_at = when
    db.add(call)
    await db.flush()


async def _credit(db, user_id, amount):
    db.add(
        CreditTransaction(
            user_id=user_id, amount=amount, amount_left=amount,
            provider=CreditTransactionProvider.revolut, status=CreditTransactionStatus.completed,
        )
    )
    await db.flush()


async def _subscribe(db, user_id, tier):
    db.add(PlanSubscription(user_id=user_id, tier=tier, provider="revolut", status="active"))
    await db.flush()


@pytest.mark.asyncio
async def test_free_user_allowed_within_windows(db):
    user = await _user(db)
    await _api_key(db, user.id)
    state = await get_allowance_state(db, user.id, now=NOW)
    assert state.allowed
    assert state.tier == "free"
    assert state.source == "tier"


@pytest.mark.asyncio
async def test_free_user_blocked_when_5h_exhausted_then_allowed_after_roll(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    # free 5h window is 0.5 credits — use it all 1h ago.
    await _use(db, key.id, 0.5, NOW - timedelta(hours=1))

    blocked = await get_allowance_state(db, user.id, now=NOW)
    assert not blocked.allowed
    assert blocked.source == "blocked"

    # 6h later the usage has rolled out of the 5h window.
    rolled = await get_allowance_state(db, user.id, now=NOW + timedelta(hours=6))
    assert rolled.allowed
    assert rolled.source == "tier"


@pytest.mark.asyncio
async def test_weekly_cap_independent_of_5h(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    # Spread weekly usage to exceed 2.0 weekly but keep last 5h clear.
    await _use(db, key.id, 1.5, NOW - timedelta(days=2))
    await _use(db, key.id, 1.0, NOW - timedelta(days=1))  # weekly total 2.5 > 2.0

    state = await get_allowance_state(db, user.id, now=NOW)
    assert state.weekly_used == pytest.approx(2.5)
    assert state.window_5h_used == 0.0
    assert state.source == "blocked"  # weekly exhausted, no prepaid


@pytest.mark.asyncio
async def test_paid_tier_gets_larger_windows(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    await _subscribe(db, user.id, "pro")
    await _use(db, key.id, 0.5, NOW - timedelta(hours=1))  # would exhaust free, fine for pro

    state = await get_allowance_state(db, user.id, now=NOW)
    assert state.tier == "pro"
    assert state.window_5h_limit == 8.0
    assert state.source == "tier"


@pytest.mark.asyncio
async def test_prepaid_overflow_when_windows_exhausted(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    await _use(db, key.id, 0.5, NOW - timedelta(hours=1))  # exhaust free 5h
    await _credit(db, user.id, 5.0)

    state = await get_allowance_state(db, user.id, now=NOW)
    assert state.allowed
    assert state.source == "prepaid"
    assert state.prepaid_balance == 5.0


@pytest.mark.asyncio
async def test_blocked_when_both_exhausted(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    await _use(db, key.id, 0.5, NOW - timedelta(hours=1))
    await _credit(db, user.id, 0.01)  # below PREPAID_MIN

    state = await get_allowance_state(db, user.id, now=NOW)
    assert not state.allowed
    assert state.source == "blocked"
