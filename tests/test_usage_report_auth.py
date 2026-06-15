"""Authorization invariants for POST /api-keys/admin/usage ("usage report by bearer API key").

This endpoint has no admin token on purpose: the caller authenticates by possessing the
user API key it reports usage for (sent as the request body's ``key``). The ``/admin`` path
prefix is legacy and misleading — it is NOT an admin route. These tests lock down the two
properties that keep that design from being a hole:

  (1) An unknown key registers nothing and returns 404 — you cannot meter (or implicitly
      create) a key you don't already hold.
  (2) Reporting usage for key A never touches key B — only the supplied key is metered;
      there is no parameter that lets a caller charge a different key.

Both run against the committed test DB via the async_client fixture (same path the production
gateway uses) and clean up their own rows.
"""

import uuid

import pytest
from sqlalchemy import delete, func, select

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_email

pytestmark = pytest.mark.asyncio

_FIXED_PRICE = 3.0


async def _fake_calculate_price(**_kwargs) -> float:
    return _FIXED_PRICE


async def _seed_user_with_api_key(email: str, prepaid: float):
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, email)
        await db.commit()
        user_id = user.id

    await CreditService.add_credits_for_user(user_id, prepaid, CreditTransactionProvider.voucher)
    api_key = await ApiKeyService.create_api_key(user_id=user_id, name="report-auth", user_address=None)
    return user_id, api_key


async def _inference_call_count(api_key_id) -> int:
    async with AsyncSessionLocal() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InferenceCall).where(InferenceCall.api_key_id == api_key_id)
            )
        ).scalar()
    return int(count or 0)


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_unknown_key_registers_nothing_and_returns_404(monkeypatch, async_client):
    """A key that does not exist cannot be metered: 404, and no InferenceCall row appears
    anywhere (the endpoint never implicitly creates a key from a usage report)."""
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    unknown_key = f"sk-does-not-exist-{uuid.uuid4().hex}"

    async with AsyncSessionLocal() as db:
        total_before = (await db.execute(select(func.count()).select_from(InferenceCall))).scalar()

    resp = await async_client.post(
        "/api-keys/admin/usage",
        json={
            "key": unknown_key,
            "model_name": "test-text-model",
            "input_tokens": 100,
            "output_tokens": 200,
            "cached_tokens": 0,
        },
    )
    assert resp.status_code == 404, f"Expected 404 for unknown key, got {resp.status_code}: {resp.text}"

    # No key was created, and no usage was recorded for the bogus key.
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(ApiKeyDB).where(ApiKeyDB.key == unknown_key))).scalars().first() is None
        total_after = (await db.execute(select(func.count()).select_from(InferenceCall))).scalar()
    assert total_after == total_before, "An unknown key must not register any inference call"


async def test_only_the_supplied_key_is_metered(monkeypatch, async_client):
    """Reporting usage for key A meters key A and only key A: a second, unrelated key B owned
    by a different user gets zero rows. There is no parameter to charge a different key."""
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    user_a, key_a = await _seed_user_with_api_key(f"report-auth-a-{uuid.uuid4().hex}@example.com", prepaid=10.0)
    user_b, key_b = await _seed_user_with_api_key(f"report-auth-b-{uuid.uuid4().hex}@example.com", prepaid=10.0)

    try:
        a_before = await _inference_call_count(key_a.id)
        b_before = await _inference_call_count(key_b.id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": key_a.full_key,
                "model_name": "test-text-model",
                "input_tokens": 100,
                "output_tokens": 200,
                "cached_tokens": 0,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # Exactly the reported key was metered.
        assert await _inference_call_count(key_a.id) == a_before + 1
        # The unrelated key is completely untouched.
        assert await _inference_call_count(key_b.id) == b_before
    finally:
        await _cleanup(user_a)
        await _cleanup(user_b)


async def test_api_key_type_is_metered_through_the_route(monkeypatch, async_client):
    """Sanity: the supplied key (a standard api key) does get an InferenceCall through the
    route — so the 'no row' assertions above are meaningful, not vacuously true."""
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    user_id, api_key = await _seed_user_with_api_key(f"report-auth-c-{uuid.uuid4().hex}@example.com", prepaid=10.0)
    assert api_key.type == ApiKeyType.api

    try:
        before = await _inference_call_count(api_key.id)
        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": api_key.full_key,
                "model_name": "test-text-model",
                "input_tokens": 100,
                "output_tokens": 200,
                "cached_tokens": 0,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert await _inference_call_count(api_key.id) == before + 1
    finally:
        await _cleanup(user_id)
