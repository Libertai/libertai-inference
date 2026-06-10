"""Fixed-window entitlement: windows accrue then reset to 0 on expiry (not rolling)."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.credit_transaction import CreditTransaction
from src.models.entitlement_window import EntitlementWindow
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.services.entitlement import WINDOW_5H, WINDOW_WEEKLY, get_allowance_state, open_windows, window_usage_by_users

NOW = datetime(2026, 6, 3, 12, 0, 0)


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


async def _window(db, user_id, kind, started_at, expires_at):
    db.add(EntitlementWindow(user_id=user_id, kind=kind, started_at=started_at, expires_at=expires_at))
    await db.flush()


async def _use(db, api_key_id, credits, when: datetime):
    # Seeded usage simulates tier-covered calls, so the full amount counts against windows.
    call = InferenceCall(api_key_id=api_key_id, credits_used=credits, model_name="m", tier_credits_used=credits)
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
async def test_free_user_with_no_window_allowed(db):
    user = await _user(db)
    await _api_key(db, user.id)
    state = await get_allowance_state(db, user.id, now=NOW)
    assert state.allowed
    assert state.tier == "free"
    assert state.source == "tier"
    assert state.window_5h_used == 0.0


@pytest.mark.asyncio
async def test_window_snaps_to_zero_on_expiry_not_gradually(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    # Open a 5h window now and exhaust the free 0.5 allowance.
    await _window(db, user.id, WINDOW_5H, started_at=NOW, expires_at=NOW + timedelta(hours=5))
    await _use(db, key.id, 0.5, NOW + timedelta(minutes=1))

    # Still inside the window: full usage counts, user is blocked (no prepaid).
    mid = await get_allowance_state(db, user.id, now=NOW + timedelta(hours=4, minutes=59))
    assert mid.window_5h_used == 0.5
    assert mid.source == "blocked"

    # One minute later the window has expired -> usage is 0 at once (not gradual), allowed again.
    after = await get_allowance_state(db, user.id, now=NOW + timedelta(hours=5, minutes=1))
    assert after.window_5h_used == 0.0
    assert after.source == "tier"


@pytest.mark.asyncio
async def test_weekly_window_independent_of_5h(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    # Active weekly window started 2 days ago with usage exceeding the 2.0 weekly cap.
    await _window(db, user.id, WINDOW_WEEKLY, started_at=NOW - timedelta(days=2), expires_at=NOW + timedelta(days=5))
    await _use(db, key.id, 2.5, NOW - timedelta(days=1))
    # No 5h window -> 5h usage is 0.

    state = await get_allowance_state(db, user.id, now=NOW)
    assert state.weekly_used == pytest.approx(2.5)
    assert state.window_5h_used == 0.0
    assert state.source == "blocked"  # weekly exhausted, no prepaid


@pytest.mark.asyncio
async def test_paid_tier_gets_larger_window(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    await _subscribe(db, user.id, "plus")
    await _window(db, user.id, WINDOW_5H, started_at=NOW, expires_at=NOW + timedelta(hours=5))
    await _use(db, key.id, 0.5, NOW + timedelta(minutes=1))  # would exhaust free, fine for plus

    state = await get_allowance_state(db, user.id, now=NOW + timedelta(hours=1))
    assert state.tier == "plus"
    assert state.window_5h_limit == 5.0
    assert state.source == "tier"


@pytest.mark.asyncio
async def test_prepaid_overflow_when_window_exhausted(db):
    user = await _user(db)
    key = await _api_key(db, user.id)
    await _window(db, user.id, WINDOW_5H, started_at=NOW, expires_at=NOW + timedelta(hours=5))
    await _use(db, key.id, 0.5, NOW + timedelta(minutes=1))
    await _credit(db, user.id, 5.0)

    state = await get_allowance_state(db, user.id, now=NOW + timedelta(hours=1))
    assert state.allowed
    assert state.source == "prepaid"
    assert state.prepaid_balance == 5.0


@pytest.mark.asyncio
async def test_open_windows_creates_resets_and_preserves(db):
    user = await _user(db)

    # 1. Creates fresh windows when none exist.
    await open_windows(db, user.id, now=NOW)
    w5 = (
        await db.execute(
            select(EntitlementWindow).where(EntitlementWindow.user_id == user.id, EntitlementWindow.kind == WINDOW_5H)
        )
    ).scalar_one_or_none()
    assert w5 is not None

    # 2. An active window is left untouched (same start) on a later message.
    await open_windows(db, user.id, now=NOW + timedelta(hours=1))
    w5_active = (
        await db.execute(select(EntitlementWindow).where(EntitlementWindow.user_id == user.id, EntitlementWindow.kind == WINDOW_5H))
    ).scalar_one()
    assert w5_active.started_at == NOW  # unchanged

    # 3. An expired window is reset to the new message time.
    await open_windows(db, user.id, now=NOW + timedelta(hours=6))
    w5_reset = (
        await db.execute(select(EntitlementWindow).where(EntitlementWindow.user_id == user.id, EntitlementWindow.kind == WINDOW_5H))
    ).scalar_one()
    assert w5_reset.started_at == NOW + timedelta(hours=6)
    assert w5_reset.expires_at == NOW + timedelta(hours=11)


@pytest.mark.asyncio
async def test_chat_key_usage_counts_in_window(db):
    user = await _user(db)
    chat_key = ApiKeyDB(key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, user_id=user.id, type=ApiKeyType.chat)
    db.add(chat_key)
    await db.flush()

    await open_windows(db, user.id, now=NOW)
    await _use(db, chat_key.id, 1.5, NOW)

    result = await window_usage_by_users(db, {user.id}, WINDOW_WEEKLY, NOW)
    assert result.get(user.id) == pytest.approx(1.5)

    # Same widened filter must propagate through get_allowance_state.
    state = await get_allowance_state(db, user.id, now=NOW)
    assert state.weekly_used == pytest.approx(1.5)
    assert state.window_5h_used == pytest.approx(1.5)
