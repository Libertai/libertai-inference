"""Monthly extra-credit cap: overflow aggregation + cap-aware allowance state.

Runs against the committed DB with real ``datetime.now()``; each test cleans up its rows.
"""

import uuid
from datetime import datetime, timedelta

import pydantic
import pytest
from sqlalchemy import delete

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.auth import UpdateProfileRequest
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.interfaces.payments import SubscriptionResponse
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.entitlement_window import EntitlementWindow
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.entitlement import (
    WINDOW_5H,
    WINDOW_WEEKLY,
    current_month_bounds,
    effective_prepaid,
    get_allowance_state,
    month_overflow_by_users,
)
from src.services.users import update_user_profile

pytestmark = pytest.mark.asyncio


def _last_month(now: datetime) -> datetime:
    # Explicit month arithmetic — timedelta(days=30) straddles boundaries on the 1st.
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return datetime(year, month, 15)


async def _setup(*, prepaid=0.0, cap=None, overflow_calls=(), exhaust_windows=False):
    """User + api key, with optional prepaid balance, cap, and overflow inference calls.

    ``overflow_calls``: list of (credits_used, tier_credits_used, used_at) tuples.
    ``exhaust_windows``: open both windows now and seed tier usage above the free limits
    (free tier: 0.5 per 5h window) so the next call must come from prepaid.
    Returns (user_id, key_id).
    """
    now = datetime.now()
    async with AsyncSessionLocal() as db:
        user = User(email=f"cap-{uuid.uuid4().hex}@example.com")
        user.monthly_extra_credit_cap = cap
        db.add(user)
        await db.flush()
        key = ApiKeyDB(key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, user_id=user.id, type=ApiKeyType.api)
        db.add(key)
        await db.flush()
        if exhaust_windows:
            for kind in (WINDOW_5H, WINDOW_WEEKLY):
                db.add(
                    EntitlementWindow(
                        user_id=user.id, kind=kind,
                        started_at=now - timedelta(hours=1), expires_at=now + timedelta(hours=4),
                    )
                )
            call = InferenceCall(api_key_id=key.id, credits_used=10.0, model_name="m", tier_credits_used=10.0)
            call.used_at = now - timedelta(minutes=30)
            db.add(call)
        for credits_used, tier_credits_used, used_at in overflow_calls:
            call = InferenceCall(
                api_key_id=key.id, credits_used=credits_used, model_name="m",
                tier_credits_used=tier_credits_used,
            )
            call.used_at = used_at
            db.add(call)
        if prepaid:
            db.add(
                CreditTransaction(
                    user_id=user.id, amount=prepaid, amount_left=prepaid,
                    provider=CreditTransactionProvider.revolut, status=CreditTransactionStatus.completed,
                )
            )
        await db.commit()
        return user.id, key.id


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(EntitlementWindow).where(EntitlementWindow.user_id == user_id))
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


def test_current_month_bounds():
    first, nxt = current_month_bounds(datetime(2026, 7, 9, 13, 37))
    assert first == datetime(2026, 7, 1)
    assert nxt == datetime(2026, 8, 1)
    first, nxt = current_month_bounds(datetime(2026, 12, 31, 23, 59))
    assert first == datetime(2026, 12, 1)
    assert nxt == datetime(2027, 1, 1)


def test_effective_prepaid():
    assert effective_prepaid(100.0, None, 999.0) == 100.0  # no cap -> raw balance
    assert effective_prepaid(100.0, 20.0, 5.0) == 15.0  # cap remaining binds
    assert effective_prepaid(10.0, 20.0, 5.0) == 10.0  # balance binds
    assert effective_prepaid(100.0, 20.0, 25.0) == 0.0  # overshoot floors at 0


async def test_month_overflow_sums_current_month_only():
    now = datetime.now()
    user_id, _ = await _setup(
        overflow_calls=[
            (1.0, 0.25, now - timedelta(minutes=5)),  # 0.75 overflow this month
            (2.0, 0.0, now - timedelta(minutes=10)),  # 2.0 overflow this month
            (5.0, 0.0, _last_month(now)),  # last month -> excluded
        ]
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await month_overflow_by_users(db, {user_id}, now)
        assert result.get(user_id, 0.0) == pytest.approx(2.75)
    finally:
        await _cleanup(user_id)


async def test_month_overflow_empty_input():
    async with AsyncSessionLocal() as db:
        assert await month_overflow_by_users(db, set(), datetime.now()) == {}


async def test_allowance_state_cap_reached_blocks_prepaid():
    now = datetime.now()
    user_id, _ = await _setup(
        prepaid=50.0, cap=2.0, exhaust_windows=True,
        overflow_calls=[(3.0, 0.0, now - timedelta(minutes=5))],  # 3.0 overflow >= 2.0 cap
    )
    try:
        async with AsyncSessionLocal() as db:
            state = await get_allowance_state(db, user_id)
        assert state.source == "blocked"
        assert state.allowed is False
        assert state.prepaid_balance == pytest.approx(50.0)  # raw, not capped
        assert state.monthly_extra_credit_cap == pytest.approx(2.0)
        # exhaust_windows seeds 10.0 tier-covered (0 overflow) + the explicit 3.0 overflow call
        assert state.extra_credits_used_this_month == pytest.approx(3.0)
    finally:
        await _cleanup(user_id)


async def test_allowance_state_cap_not_reached_allows_prepaid():
    now = datetime.now()
    user_id, _ = await _setup(
        prepaid=50.0, cap=10.0, exhaust_windows=True,
        overflow_calls=[(3.0, 0.0, now - timedelta(minutes=5))],
    )
    try:
        async with AsyncSessionLocal() as db:
            state = await get_allowance_state(db, user_id)
        assert state.source == "prepaid"
        assert state.allowed is True
    finally:
        await _cleanup(user_id)


async def test_allowance_state_cap_irrelevant_while_windows_open():
    user_id, _ = await _setup(prepaid=50.0, cap=0.01)  # tiny cap, fresh windows
    try:
        async with AsyncSessionLocal() as db:
            state = await get_allowance_state(db, user_id)
        assert state.source == "tier"
        assert state.allowed is True
    finally:
        await _cleanup(user_id)


async def test_month_overflow_excludes_shared_chat_key():
    from src.config import config

    if not config.LIBERTAI_CHAT_API_KEY:
        pytest.skip("LIBERTAI_CHAT_API_KEY not configured")
    now = datetime.now()
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        exists = (
            await db.execute(select(ApiKeyDB.id).where(ApiKeyDB.key == config.LIBERTAI_CHAT_API_KEY))
        ).scalar_one_or_none()
    if exists is not None:
        pytest.skip("shared chat key already present in this DB (key column is unique)")
    user_id, _ = await _setup(overflow_calls=[(1.0, 0.0, now - timedelta(minutes=5))])
    try:
        # Attach a key whose value IS the shared anonymous chat key to the same user;
        # its calls must not count against the user's cap.
        async with AsyncSessionLocal() as db:
            shared = ApiKeyDB(
                key=config.LIBERTAI_CHAT_API_KEY, name=uuid.uuid4().hex, user_id=user_id, type=ApiKeyType.chat
            )
            db.add(shared)
            await db.flush()
            call = InferenceCall(api_key_id=shared.id, credits_used=42.0, model_name="m", tier_credits_used=0.0)
            call.used_at = now - timedelta(minutes=5)
            db.add(call)
            await db.commit()
        async with AsyncSessionLocal() as db:
            result = await month_overflow_by_users(db, {user_id}, now)
        assert result.get(user_id, 0.0) == pytest.approx(1.0)
    finally:
        await _cleanup(user_id)


async def test_allowance_state_no_cap_unchanged():
    now = datetime.now()
    user_id, _ = await _setup(
        prepaid=50.0, cap=None, exhaust_windows=True,
        overflow_calls=[(999.0, 0.0, now - timedelta(minutes=5))],
    )
    try:
        async with AsyncSessionLocal() as db:
            state = await get_allowance_state(db, user_id)
        assert state.source == "prepaid"
        assert state.monthly_extra_credit_cap is None
    finally:
        await _cleanup(user_id)


def test_update_request_rejects_non_positive_cap():
    with pytest.raises(pydantic.ValidationError):
        UpdateProfileRequest(monthly_extra_credit_cap=0)
    with pytest.raises(pydantic.ValidationError):
        UpdateProfileRequest(monthly_extra_credit_cap=-5.0)
    with pytest.raises(pydantic.ValidationError):
        UpdateProfileRequest(monthly_extra_credit_cap=float("nan"))
    with pytest.raises(pydantic.ValidationError):
        UpdateProfileRequest(monthly_extra_credit_cap=float("inf"))
    assert UpdateProfileRequest(monthly_extra_credit_cap=12.5).monthly_extra_credit_cap == 12.5
    assert UpdateProfileRequest(monthly_extra_credit_cap=None).monthly_extra_credit_cap is None


def test_update_request_partial_dump():
    # exclude_unset is what keeps a cap-only PATCH from clobbering display_name (and vice versa).
    req = UpdateProfileRequest.model_validate({"monthly_extra_credit_cap": 5.0})
    assert req.model_dump(exclude_unset=True) == {"monthly_extra_credit_cap": 5.0}
    req = UpdateProfileRequest.model_validate({"display_name": "Ada"})
    assert req.model_dump(exclude_unset=True) == {"display_name": "Ada"}
    req = UpdateProfileRequest.model_validate({"monthly_extra_credit_cap": None})
    assert req.model_dump(exclude_unset=True) == {"monthly_extra_credit_cap": None}


async def test_update_user_profile_partial():
    user_id, _ = await _setup()
    try:
        async with AsyncSessionLocal() as db:
            await update_user_profile(db, user_id, {"display_name": "Ada", "monthly_extra_credit_cap": 7.5})
            await db.commit()
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            assert user.display_name == "Ada"
            assert user.monthly_extra_credit_cap == 7.5
        # Cap-only update leaves display_name alone.
        async with AsyncSessionLocal() as db:
            await update_user_profile(db, user_id, {"monthly_extra_credit_cap": None})
            await db.commit()
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            assert user.display_name == "Ada"
            assert user.monthly_extra_credit_cap is None
    finally:
        await _cleanup(user_id)


def test_subscription_response_carries_cap_fields():
    resp = SubscriptionResponse(
        tier="free", has_subscription=False,
        monthly_extra_credit_cap=20.0, extra_credits_used_this_month=3.5,
    )
    assert resp.monthly_extra_credit_cap == 20.0
    assert resp.extra_credits_used_this_month == 3.5
    # Defaults keep older clients working.
    resp = SubscriptionResponse(tier="free", has_subscription=False)
    assert resp.monthly_extra_credit_cap is None
    assert resp.extra_credits_used_this_month == 0.0
