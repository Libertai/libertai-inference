"""Unit tests for the warm API-key pool service.

These exercise ApiKeyPoolService against the committed test DB (the service opens
its own AsyncSessionLocal), so each test clears the pool before and after itself.
"""

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.services.api_key_pool import ApiKeyPoolService, POOL_SENTINEL_NAME

pytestmark = pytest.mark.asyncio


async def _pool_count() -> int:
    async with AsyncSessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(ApiKeyDB).where(ApiKeyDB.type == ApiKeyType.pool)
                )
            ).scalar()
            or 0
        )


async def _add_pool_key(*, age_seconds: int) -> str:
    """Insert a pool row whose created_at is `age_seconds` in the past. Returns its key."""
    async with AsyncSessionLocal() as db:
        key = ApiKeyDB.generate_key()
        row = ApiKeyDB(key=key, name=POOL_SENTINEL_NAME, type=ApiKeyType.pool)
        row.created_at = datetime.now() - timedelta(seconds=age_seconds)
        db.add(row)
        await db.commit()
        return key


async def _clear_pool() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.type == ApiKeyType.pool))
        await db.commit()


@pytest.fixture(autouse=True)
async def _isolate_pool():
    await _clear_pool()
    yield
    await _clear_pool()


async def test_ensure_pool_creates_up_to_size(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config, "POOL_SIZE", 3)
    await ApiKeyPoolService.ensure_pool()
    assert await _pool_count() == 3


async def test_ensure_pool_is_idempotent_when_full(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config, "POOL_SIZE", 3)
    await ApiKeyPoolService.ensure_pool()
    await ApiKeyPoolService.ensure_pool()
    assert await _pool_count() == 3


async def test_ensure_pool_tops_up_partial_pool(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config, "POOL_SIZE", 4)
    await _add_pool_key(age_seconds=120)
    await ApiKeyPoolService.ensure_pool()
    assert await _pool_count() == 4
