"""Gateway chokepoint: dual-window entitlement decides api-key inclusion + prepaid deduction.

These exercise ApiKeyService against the committed DB (the service opens its own
session and uses real ``datetime.now()``), so each test cleans up its own rows.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.entitlement_window import EntitlementWindow
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.entitlement import WINDOW_5H

pytestmark = pytest.mark.asyncio


async def _setup(*, usage=None, window="active", prepaid=0.0, tier=None):
    """Create a user + api key with optional usage in an active/expired 5h window.

    ``window`` controls the 5h window state when ``usage`` is given:
      - "active":  window open now, usage counts against the allowance
      - "expired": window already ended, so the usage has reset to 0
    Returns (user_id, key_str).
    """
    now = datetime.now()
    async with AsyncSessionLocal() as db:
        user = User(email=f"enf-{uuid.uuid4().hex}@example.com", email_verified=True)
        db.add(user)
        await db.flush()
        key = ApiKeyDB(key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, user_id=user.id, type=ApiKeyType.api)
        db.add(key)
        await db.flush()
        if usage:
            if window == "active":
                started, expires, used_at = now - timedelta(hours=1), now + timedelta(hours=4), now - timedelta(minutes=30)
            else:  # expired
                started, expires, used_at = now - timedelta(hours=6), now - timedelta(hours=1), now - timedelta(hours=5)
            db.add(EntitlementWindow(user_id=user.id, kind=WINDOW_5H, started_at=started, expires_at=expires))
            call = InferenceCall(api_key_id=key.id, credits_used=usage, model_name="m")
            call.used_at = used_at
            db.add(call)
        if prepaid:
            db.add(
                CreditTransaction(
                    user_id=user.id, amount=prepaid, amount_left=prepaid,
                    provider=CreditTransactionProvider.revolut, status=CreditTransactionStatus.completed,
                )
            )
        if tier:
            db.add(PlanSubscription(user_id=user.id, tier=tier, provider="revolut", status="active"))
        await db.commit()
        return user.id, key.key


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        from sqlalchemy import delete

        await db.execute(delete(EntitlementWindow).where(EntitlementWindow.user_id == user_id))
        await db.execute(delete(PlanSubscription).where(PlanSubscription.user_id == user_id))
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        # inference_calls cascade via api_keys FK; delete keys then user.
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_free_key_included_within_window():
    user_id, key = await _setup()
    try:
        assert key in await ApiKeyService.get_admin_all_api_keys()
    finally:
        await _cleanup(user_id)


async def test_free_key_excluded_when_window_exhausted_no_prepaid():
    user_id, key = await _setup(usage=0.5, window="active")  # free 5h limit is 0.5
    try:
        assert key not in await ApiKeyService.get_admin_all_api_keys()
    finally:
        await _cleanup(user_id)


async def test_key_returns_after_window_resets():
    user_id, key = await _setup(usage=0.5, window="expired")  # window ended -> usage reset to 0
    try:
        assert key in await ApiKeyService.get_admin_all_api_keys()
    finally:
        await _cleanup(user_id)


async def test_paid_tier_gets_larger_window():
    user_id, key = await _setup(usage=0.5, window="active", tier="plus")  # exhausts free, fine for plus
    try:
        assert key in await ApiKeyService.get_admin_all_api_keys()
    finally:
        await _cleanup(user_id)


async def test_prepaid_overflow_keeps_key_included():
    user_id, key = await _setup(usage=0.5, window="active", prepaid=5.0)
    try:
        assert key in await ApiKeyService.get_admin_all_api_keys()
    finally:
        await _cleanup(user_id)


async def test_prepaid_not_charged_while_within_window_then_charged_on_overflow():
    # Free tier, 5h window = 0.5 credits, prepaid 5.0.
    user_id, key = await _setup(prepaid=5.0)
    try:
        # First small call stays within the free window -> no prepaid deduction.
        await ApiKeyService.register_inference_call(key, credits_used=0.4, model_name="m")
        assert await _balance(user_id) == pytest.approx(5.0)

        # Second 0.4 call straddles the 0.5 window (0.1 left) -> only the 0.3 overflow is
        # charged, not the whole call. (Pre-fix this deducted the full 0.4 -> 4.6.)
        await ApiKeyService.register_inference_call(key, credits_used=0.4, model_name="m")
        assert await _balance(user_id) == pytest.approx(4.7)
    finally:
        await _cleanup(user_id)


async def test_pool_keys_are_included_in_whitelist_unconditionally():
    """Unclaimed pool keys must ride in the whitelist (no owner, no credits) so they
    propagate to instances and are warm by the time they're claimed."""
    from src.services.api_key_pool import POOL_SENTINEL_NAME

    async with AsyncSessionLocal() as db:
        pool_key = ApiKeyDB.generate_key()
        row = ApiKeyDB(key=pool_key, name=POOL_SENTINEL_NAME, type=ApiKeyType.pool)
        db.add(row)
        await db.commit()
    try:
        assert pool_key in await ApiKeyService.get_admin_all_api_keys()
    finally:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import delete

            await db.execute(delete(ApiKeyDB).where(ApiKeyDB.key == pool_key))
            await db.commit()


async def _balance(user_id) -> float:
    from sqlalchemy import func, select

    async with AsyncSessionLocal() as db:
        total = (
            await db.execute(
                select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.status == CreditTransactionStatus.completed,
                )
            )
        ).scalar()
    return float(total or 0.0)
