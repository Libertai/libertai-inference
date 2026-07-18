"""Invalid-key classification in the admin list (reason map distributed to the gateway).

Service opens its own session with real datetime.now(); each test cleans its rows.
"""

from datetime import datetime, timedelta

import pytest

from src.interfaces.api_keys import InvalidKeyReason
from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.services.api_key import ApiKeyService
from tests.test_admin_list_enforcement import _cleanup, _cleanup_liberclaw, _setup, _setup_liberclaw

pytestmark = pytest.mark.asyncio


async def _mutate_key(key_str, **attrs):
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(ApiKeyDB).where(ApiKeyDB.key == key_str))).scalars().one()
        for name, value in attrs.items():
            setattr(row, name, value)
        await db.commit()


async def _admin():
    return await ApiKeyService.get_admin_all_api_keys()


async def test_disabled_key_reported():
    user_id, key = await _setup()
    await _mutate_key(key, is_active=False)
    try:
        res = await _admin()
        assert key not in res.valid
        assert res.invalid[key].reason == InvalidKeyReason.disabled
        assert res.invalid[key].message
    finally:
        await _cleanup(user_id)


async def test_recently_expired_key_reported():
    user_id, key = await _setup()
    await _mutate_key(key, expires_at=datetime.now() - timedelta(days=1))
    try:
        res = await _admin()
        assert key not in res.valid
        assert res.invalid[key].reason == InvalidKeyReason.expired
    finally:
        await _cleanup(user_id)


async def test_long_expired_key_pruned_from_both_lists():
    user_id, key = await _setup()
    await _mutate_key(key, expires_at=datetime.now() - timedelta(days=31))
    try:
        res = await _admin()
        assert key not in res.valid
        assert key not in res.invalid
    finally:
        await _cleanup(user_id)


async def test_deleted_key_in_neither_list():
    user_id, key = await _setup()
    await _mutate_key(key, is_active=False, deleted_at=datetime.now())
    try:
        res = await _admin()
        assert key not in res.valid
        assert key not in res.invalid
    finally:
        await _cleanup(user_id)


async def test_no_credits_reason():
    # Free 5h window exhausted (limit 0.5), no prepaid.
    user_id, key = await _setup(usage=0.5, window="active")
    try:
        res = await _admin()
        assert res.invalid[key].reason == InvalidKeyReason.no_credits
    finally:
        await _cleanup(user_id)


async def test_extra_credit_cap_reason():
    # Window exhausted; prepaid exists but monthly cap consumed by overflow.
    user_id, key = await _setup(usage=0.5, window="active", prepaid=50.0, cap=2.0, overflow=3.0)
    try:
        res = await _admin()
        assert res.invalid[key].reason == InvalidKeyReason.extra_credit_cap
    finally:
        await _cleanup(user_id)


async def test_key_monthly_limit_reason():
    user_id, key = await _setup()
    await _mutate_key(key, monthly_limit=0.0)
    try:
        res = await _admin()
        assert res.invalid[key].reason == InvalidKeyReason.key_monthly_limit
    finally:
        await _cleanup(user_id)


async def test_liberclaw_limit_reason():
    limit = LIBERCLAW_TIERS["free"]["credits_limit"]
    lc_id, key = await _setup_liberclaw(usage=limit + 1)
    try:
        res = await _admin()
        assert res.invalid[key].reason == InvalidKeyReason.liberclaw_limit
    finally:
        await _cleanup_liberclaw(lc_id)


async def test_valid_key_not_in_invalid_map():
    user_id, key = await _setup()
    try:
        res = await _admin()
        assert key in res.valid
        assert key not in res.invalid
    finally:
        await _cleanup(user_id)


async def test_admin_route_serializes_invalid_map():
    from src.interfaces.api_keys import ApiKeyAdminListResponse

    user_id, key = await _setup()
    await _mutate_key(key, is_active=False)
    try:
        res = await _admin()
        payload = ApiKeyAdminListResponse(keys=res.valid, invalid_keys=res.invalid).model_dump()
        assert payload["invalid_keys"][key] == {
            "reason": "disabled",
            "message": "This API key has been disabled.",
        }
    finally:
        await _cleanup(user_id)
