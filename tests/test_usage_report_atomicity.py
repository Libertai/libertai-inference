"""Atomicity of the usage report: metering rows, chat history, and the overflow deduction.

A failure anywhere must roll back the whole report. The reporting gateway retries usage
reports (the call already happened), so a partially-committed report would be duplicated
on retry. These tests prove a retry after a failure registers exactly one row.
"""

import pytest
from sqlalchemy import delete, func, select

from src.interfaces.credits import CreditTransactionProvider
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.credit_transaction import CreditTransaction
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.chat_request import ChatRequestService
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_email, get_or_create_user_by_wallet
from src.subscription_tiers import get_tier

pytestmark = pytest.mark.asyncio

# A call that always overflows the free 5h window, so the prepaid deduction runs.
_OVERFLOW_CALL = get_tier("free").window_5h_credits + 1.0


async def _seed_user_by_wallet(address: str, prepaid: float):
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id
    await CreditService.add_credits_for_user(user_id, prepaid, CreditTransactionProvider.voucher)
    return user_id


async def _seed_user_by_email(email: str, prepaid: float):
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, email)
        await db.commit()
        user_id = user.id
    await CreditService.add_credits_for_user(user_id, prepaid, CreditTransactionProvider.voucher)
    return user_id


async def _balance(user_id) -> float:
    from src.interfaces.credits import CreditTransactionStatus

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


async def _inference_call_count(api_key_id) -> int:
    async with AsyncSessionLocal() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InferenceCall).where(InferenceCall.api_key_id == api_key_id)
            )
        ).scalar()
    return int(count or 0)


async def _chat_request_count(api_key_id) -> int:
    async with AsyncSessionLocal() as db:
        count = (
            await db.execute(select(func.count()).select_from(ChatRequest).where(ChatRequest.api_key_id == api_key_id))
        ).scalar()
    return int(count or 0)


async def _cleanup(user_id, api_key_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(ChatRequest).where(ChatRequest.api_key_id == api_key_id))
        await db.execute(delete(InferenceCall).where(InferenceCall.api_key_id == api_key_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_deduction_failure_rolls_back_usage_row(monkeypatch):
    """A failure in the overflow deduction must roll back the usage row too, so the
    reporting gateway's retry registers exactly one row. Falsifiable: pre-fix the row
    was committed before the deduction, leaving it behind — and a retry added a second."""
    address = "0xA9100000000000000000000000000000000000051"
    user_id = await _seed_user_by_wallet(address, prepaid=10.0)
    api_key = await ApiKeyService.create_api_key(user_id=user_id, name="atomic", user_address=address)

    try:
        original = CreditService.use_credits
        state = {"raise": True}

        async def _flaky_use_credits(*args, **kwargs):
            if state["raise"]:
                raise RuntimeError("deduction failed")
            return await original(*args, **kwargs)

        monkeypatch.setattr(CreditService, "use_credits", _flaky_use_credits)

        with pytest.raises(RuntimeError, match="deduction failed"):
            await ApiKeyService.register_inference_call(
                key=api_key.full_key, credits_used=_OVERFLOW_CALL, model_name="m"
            )

        assert await _inference_call_count(api_key.id) == 0  # nothing left behind
        assert await _balance(user_id) == pytest.approx(10.0)  # no partial deduction persisted

        # The gateway retries after the failed report: exactly one usage row this time.
        state["raise"] = False
        ok = await ApiKeyService.register_inference_call(
            key=api_key.full_key, credits_used=_OVERFLOW_CALL, model_name="m"
        )
        assert ok is True
        assert await _inference_call_count(api_key.id) == 1
        assert await _balance(user_id) == pytest.approx(9.0)  # overflow deducted exactly once
    finally:
        await _cleanup(user_id, api_key.id)


async def test_chat_history_failure_rolls_back_metering(monkeypatch, async_client):
    """A failure recording the chat history must roll back the metering as one transaction,
    so the gateway's retry meters exactly once. Falsifiable: pre-fix the InferenceCall row
    was committed before the ChatRequest, so a retry duplicated it."""
    import src.routes.api_keys.api_keys as route_module

    async def _fake_calculate_price(**_kwargs) -> float:
        return 3.0

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    email = "usage-atomicity@example.com"
    user_id = await _seed_user_by_email(email, prepaid=10.0)
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=None)
    key_id = chat_key.id

    try:
        original = ChatRequestService.add_chat_request
        state = {"raise": True}

        async def _flaky_add_chat_request(*args, **kwargs):
            if state["raise"]:
                raise RuntimeError("chat history failed")
            return await original(*args, **kwargs)

        monkeypatch.setattr(ChatRequestService, "add_chat_request", _flaky_add_chat_request)

        usage_payload = {
            "key": chat_key.full_key,
            "model_name": "test-text-model",
            "input_tokens": 100,
            "output_tokens": 200,
            "cached_tokens": 0,
        }

        resp = await async_client.post("/api-keys/admin/usage", json=usage_payload)
        assert resp.status_code == 500, f"Expected 500, got {resp.status_code}: {resp.text}"
        assert await _inference_call_count(key_id) == 0  # metering rolled back with the history
        assert await _chat_request_count(key_id) == 0

        # The gateway retries after the failure: exactly one row each.
        state["raise"] = False
        resp = await async_client.post("/api-keys/admin/usage", json=usage_payload)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert await _inference_call_count(key_id) == 1
        assert await _chat_request_count(key_id) == 1
    finally:
        # Cleanup (async_client fixture has no per-test rollback).
        await _cleanup(user_id, key_id)
