"""Post-call key state returned by POST /api-keys/admin/usage.

Metering is post-hoc: the model server that served a call reports it here, and the answer
tells it whether the key it just used is still usable. Without that, a key which ran out
mid-window keeps being served until the next whitelist push. The reasons must match what
``get_admin_all_api_keys`` would distribute for the same key.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from src.interfaces.api_keys import ApiKeyType, InvalidKeyReason
from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.services.api_key import ApiKeyService
from tests.test_admin_list_enforcement import FREE_5H, _cleanup, _cleanup_liberclaw, _setup, _setup_liberclaw
from tests.test_admin_list_invalid_reasons import _mutate_key

pytestmark = pytest.mark.asyncio


async def _fake_calculate_price(**_kwargs) -> float:
    return FREE_5H


async def test_usable_key_reports_nothing():
    user_id, key = await _setup()
    try:
        assert await ApiKeyService.get_invalid_key_info(key) is None
    finally:
        await _cleanup(user_id)


async def test_exhausted_window_reports_no_credits():
    user_id, key = await _setup(usage=FREE_5H, window="active")
    try:
        info = await ApiKeyService.get_invalid_key_info(key)
        assert info is not None
        assert info.reason == InvalidKeyReason.no_credits
        assert info.message
    finally:
        await _cleanup(user_id)


async def test_prepaid_balance_keeps_key_usable_past_the_window():
    user_id, key = await _setup(usage=FREE_5H, window="active", prepaid=50.0)
    try:
        assert await ApiKeyService.get_invalid_key_info(key) is None
    finally:
        await _cleanup(user_id)


async def test_capped_extra_credits_report_cap_reason():
    user_id, key = await _setup(usage=FREE_5H, window="active", prepaid=50.0, cap=2.0, overflow=3.0)
    try:
        info = await ApiKeyService.get_invalid_key_info(key)
        assert info is not None
        assert info.reason == InvalidKeyReason.extra_credit_cap
    finally:
        await _cleanup(user_id)


async def test_key_monthly_limit_reported():
    user_id, key = await _setup()
    await _mutate_key(key, monthly_limit=0.0)
    try:
        info = await ApiKeyService.get_invalid_key_info(key)
        assert info is not None
        assert info.reason == InvalidKeyReason.key_monthly_limit
    finally:
        await _cleanup(user_id)


async def test_disabled_key_reported():
    user_id, key = await _setup()
    await _mutate_key(key, is_active=False)
    try:
        info = await ApiKeyService.get_invalid_key_info(key)
        assert info is not None
        assert info.reason == InvalidKeyReason.disabled
    finally:
        await _cleanup(user_id)


async def test_expired_key_reported():
    user_id, key = await _setup()
    await _mutate_key(key, expires_at=datetime.now() - timedelta(days=1))
    try:
        info = await ApiKeyService.get_invalid_key_info(key)
        assert info is not None
        assert info.reason == InvalidKeyReason.expired
    finally:
        await _cleanup(user_id)


async def test_liberclaw_limit_reported():
    limit = LIBERCLAW_TIERS["free"]["credits_limit"]
    lc_id, key = await _setup_liberclaw(usage=limit + 1)
    try:
        info = await ApiKeyService.get_invalid_key_info(key)
        assert info is not None
        assert info.reason == InvalidKeyReason.liberclaw_limit
    finally:
        await _cleanup_liberclaw(lc_id)


async def test_liberclaw_key_within_limit_not_reported():
    lc_id, key = await _setup_liberclaw(usage=1.0)
    try:
        assert await ApiKeyService.get_invalid_key_info(key) is None
    finally:
        await _cleanup_liberclaw(lc_id)


async def test_internal_key_never_reported():
    """x402 keys carry their own payment auth: no user-facing reason to hand back."""
    async with AsyncSessionLocal() as db:
        key_row = ApiKeyDB(key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, type=ApiKeyType.x402)
        key_row.is_active = False
        db.add(key_row)
        await db.commit()
        key = key_row.key
    try:
        assert await ApiKeyService.get_invalid_key_info(key) is None
    finally:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import delete

            await db.execute(delete(ApiKeyDB).where(ApiKeyDB.key == key))
            await db.commit()


async def test_unknown_key_reports_nothing():
    assert await ApiKeyService.get_invalid_key_info(f"sk-nope-{uuid.uuid4().hex}") is None


async def test_route_reports_the_call_that_exhausts_the_window(monkeypatch, async_client):
    """The straddling call itself must come back blocked — that is the call after which
    every further request would be served for free."""
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)
    user_id, key = await _setup()
    try:
        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={"key": key, "model_name": "m", "input_tokens": 10, "output_tokens": 10, "cached_tokens": 0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["invalid"]["reason"] == InvalidKeyReason.no_credits.value
    finally:
        await _cleanup(user_id)


async def test_route_reports_nothing_while_the_key_is_usable(monkeypatch, async_client):
    import src.routes.api_keys.api_keys as route_module

    async def _cheap(**_kwargs) -> float:
        return FREE_5H / 10

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _cheap)
    user_id, key = await _setup()
    try:
        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={"key": key, "model_name": "m", "input_tokens": 10, "output_tokens": 10, "cached_tokens": 0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"invalid": None}
    finally:
        await _cleanup(user_id)
