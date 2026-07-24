"""Each new-string creation path hands out a pre-warmed pool key when one is ready.

Services open their own AsyncSessionLocal and commit to the test DB, so these tests
clear the pool and clean up created rows themselves.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.api_key_pool import POOL_SENTINEL_NAME

pytestmark = pytest.mark.asyncio


async def _add_warm_pool_key() -> str:
    async with AsyncSessionLocal() as db:
        key = ApiKeyDB.generate_key()
        row = ApiKeyDB(key=key, name=POOL_SENTINEL_NAME, type=ApiKeyType.pool)
        row.created_at = datetime.now() - timedelta(seconds=120)
        db.add(row)
        await db.commit()
        return key


async def _clear_pool() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.type == ApiKeyType.pool))
        await db.commit()


async def _make_user() -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        user = User(email=f"warm-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.commit()
        return user.id


async def _cleanup_user(user_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


@pytest.fixture(autouse=True)
async def _isolate_pool():
    await _clear_pool()
    yield
    await _clear_pool()


async def test_create_api_key_uses_warm_pool_key(monkeypatch):
    # Stop the post-claim background refill from racing this test's pool assertions.
    from src.services import api_key_pool

    monkeypatch.setattr(api_key_pool.ApiKeyPoolService, "schedule_refill", staticmethod(lambda: None))

    warm = await _add_warm_pool_key()
    user_id = await _make_user()
    try:
        result = await ApiKeyService.create_api_key(user_id=user_id, name="warm-test")
        assert result.full_key == warm  # got the pre-warmed string, not a cold one
        assert result.type == ApiKeyType.api
    finally:
        await _cleanup_user(user_id)


async def test_create_api_key_falls_back_to_cold_when_pool_empty(monkeypatch):
    from src.services import api_key_pool

    monkeypatch.setattr(api_key_pool.ApiKeyPoolService, "schedule_refill", staticmethod(lambda: None))

    user_id = await _make_user()
    try:
        result = await ApiKeyService.create_api_key(user_id=user_id, name="cold-test")
        assert len(result.full_key) == 32  # a freshly generated hex key
        assert result.type == ApiKeyType.api
    finally:
        await _cleanup_user(user_id)


async def test_cli_create_uses_warm_pool_key(monkeypatch):
    from src.services import api_key_pool

    monkeypatch.setattr(api_key_pool.ApiKeyPoolService, "schedule_refill", staticmethod(lambda: None))

    warm = await _add_warm_pool_key()
    user_id = await _make_user()
    try:
        result = await ApiKeyService.rotate_or_create_cli_api_key(user_id=user_id, host="laptop")
        assert result.full_key == warm
        assert result.type == ApiKeyType.cli
        assert result.expires_at is not None
    finally:
        await _cleanup_user(user_id)


async def test_cli_rotate_adopts_warm_string_in_place(monkeypatch):
    from src.services import api_key_pool

    monkeypatch.setattr(api_key_pool.ApiKeyPoolService, "schedule_refill", staticmethod(lambda: None))

    user_id = await _make_user()
    try:
        # First mint with an empty pool -> cold key on a brand-new row.
        first = await ApiKeyService.rotate_or_create_cli_api_key(user_id=user_id, host="laptop")
        original_id = first.id

        # Now a warm key is ready; rotating must adopt the warm string into the SAME row.
        warm = await _add_warm_pool_key()
        second = await ApiKeyService.rotate_or_create_cli_api_key(user_id=user_id, host="laptop")
        assert second.id == original_id  # same row -> usage history preserved
        assert second.full_key == warm  # adopted the warm, already-propagated string
        # the consumed pool row is gone
        assert await _pool_count_cli_helper() == 0
    finally:
        await _cleanup_user(user_id)


async def test_chat_create_uses_warm_pool_key(monkeypatch):
    from src.services import api_key_pool

    monkeypatch.setattr(api_key_pool.ApiKeyPoolService, "schedule_refill", staticmethod(lambda: None))

    warm = await _add_warm_pool_key()
    user_id = await _make_user()
    try:
        result = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id)
        assert result.full_key == warm
        assert result.type == ApiKeyType.chat
    finally:
        await _cleanup_user(user_id)


async def test_chat_get_existing_does_not_consume_pool(monkeypatch):
    from src.services import api_key_pool

    monkeypatch.setattr(api_key_pool.ApiKeyPoolService, "schedule_refill", staticmethod(lambda: None))

    # First create (empty pool -> cold), then add a warm key and fetch again:
    # the second call must return the EXISTING chat key, not consume the pool.
    user_id = await _make_user()
    try:
        first = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id)
        warm = await _add_warm_pool_key()
        second = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id)
        assert second.full_key == first.full_key  # existing key returned
        assert second.full_key != warm
        assert await _pool_count_cli_helper() == 1  # pool untouched
    finally:
        await _cleanup_user(user_id)


async def test_liberclaw_create_uses_warm_pool_key(monkeypatch):
    from src.models.liberclaw_user import LiberclawUser
    from src.services import api_key_pool
    from src.services.liberclaw import LiberclawService

    monkeypatch.setattr(api_key_pool.ApiKeyPoolService, "schedule_refill", staticmethod(lambda: None))

    warm = await _add_warm_pool_key()
    user_id = f"lc-{uuid.uuid4().hex}"
    try:
        result = await LiberclawService.get_or_create_api_key(user_id=user_id, user_type="discord")
        assert result.is_new is True
        assert result.key == warm  # warm, already-propagated string
    finally:
        async with AsyncSessionLocal() as db:
            lc = (
                await db.execute(
                    __import__("sqlalchemy").select(LiberclawUser).where(LiberclawUser.user_id == user_id)
                )
            ).scalars().first()
            if lc is not None:
                await db.execute(delete(ApiKeyDB).where(ApiKeyDB.liberclaw_user_id == lc.id))
                await db.execute(delete(LiberclawUser).where(LiberclawUser.id == lc.id))
                await db.commit()


async def _pool_count_cli_helper() -> int:
    from sqlalchemy import func, select

    async with AsyncSessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(ApiKeyDB).where(ApiKeyDB.type == ApiKeyType.pool)
                )
            ).scalar()
            or 0
        )
