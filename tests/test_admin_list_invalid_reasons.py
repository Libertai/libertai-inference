"""Invalid-key classification in the admin list (reason map distributed to the gateway).

Service opens its own session with real datetime.now(); each test cleans its rows.
"""

from datetime import datetime, timedelta

import pytest

from src.interfaces.api_keys import ApiKeyType, InvalidKeyReason
from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.services.api_key import ApiKeyService
from src.services.api_key_pool import POOL_SENTINEL_NAME
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


async def _delete_key(key_str):
    from sqlalchemy import delete

    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.key == key_str))
        await db.commit()


async def test_ownerless_liberclaw_key_in_neither_list():
    # liberclaw_user_id=None: ownership-broken, not user-explainable -> generic 401, not surfaced.
    key_str = ApiKeyDB.generate_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKeyDB(key=key_str, name="ownerless-liberclaw", type=ApiKeyType.liberclaw, liberclaw_user_id=None))
        await db.commit()
    try:
        res = await _admin()
        assert key_str not in res.valid
        assert key_str not in res.invalid
    finally:
        await _delete_key(key_str)


async def test_ownerless_api_key_in_neither_list():
    # user_id=None: ownership-broken, not user-explainable -> generic 401, not surfaced.
    key_str = ApiKeyDB.generate_key()
    async with AsyncSessionLocal() as db:
        db.add(ApiKeyDB(key=key_str, name="ownerless-api", type=ApiKeyType.api, user_id=None))
        await db.commit()
    try:
        res = await _admin()
        assert key_str not in res.valid
        assert key_str not in res.invalid
    finally:
        await _delete_key(key_str)


async def test_disabled_x402_key_in_neither_list():
    # x402 keys are internal (own payment auth) -> never surfaced with a user-facing reason.
    key_str = ApiKeyDB.generate_key()
    async with AsyncSessionLocal() as db:
        row = ApiKeyDB(key=key_str, name="disabled-x402", type=ApiKeyType.x402)
        row.is_active = False
        db.add(row)
        await db.commit()
    try:
        res = await _admin()
        assert key_str not in res.valid
        assert key_str not in res.invalid
    finally:
        await _delete_key(key_str)


async def test_disabled_pool_key_reported_invalid():
    # Pool keys aren't ownership- or x402-exempt, so a disabled one is reported like any
    # other disabled key (reason=disabled) rather than silently dropped.
    key_str = ApiKeyDB.generate_key()
    async with AsyncSessionLocal() as db:
        row = ApiKeyDB(key=key_str, name=POOL_SENTINEL_NAME, type=ApiKeyType.pool)
        row.is_active = False
        db.add(row)
        await db.commit()
    try:
        res = await _admin()
        assert key_str not in res.valid
        assert res.invalid[key_str].reason == InvalidKeyReason.disabled
    finally:
        await _delete_key(key_str)
