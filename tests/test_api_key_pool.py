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
from src.services.api_key_pool import POOL_SENTINEL_NAME, ApiKeyPoolService

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


async def test_claim_returns_warm_row_and_resets_metadata():
    warm = await _add_pool_key(age_seconds=120)
    async with AsyncSessionLocal() as db:
        # Need a real user row for the FK; create a throwaway user.
        from src.models.user import User

        user = User(email=f"pool-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.flush()
        row = await ApiKeyPoolService.claim_warm_key(
            db, target_type=ApiKeyType.api, user_id=user.id, name="my-key", monthly_limit=12.5
        )
        await db.commit()
        assert row is not None
        assert row.key == warm  # same already-propagated string
        assert row.type == ApiKeyType.api
        assert row.user_id == user.id
        assert row.name == "my-key"
        assert row.monthly_limit == 12.5
        assert (datetime.now() - row.created_at) < timedelta(seconds=30)
        # cleanup the claimed key + user
        await db.delete(row)
        await db.delete(user)
        await db.commit()


async def test_claim_returns_none_when_no_ready_pool_key():
    # Fresh pool key (age 0) is younger than the warm threshold -> not claimable.
    await _add_pool_key(age_seconds=0)
    async with AsyncSessionLocal() as db:
        row = await ApiKeyPoolService.claim_warm_key(
            db, target_type=ApiKeyType.api, user_id=uuid.uuid4(), name="x"
        )
        await db.rollback()
        assert row is None


async def test_claim_returns_none_when_pool_empty():
    async with AsyncSessionLocal() as db:
        row = await ApiKeyPoolService.claim_warm_key(
            db, target_type=ApiKeyType.api, user_id=uuid.uuid4(), name="x"
        )
        await db.rollback()
        assert row is None


async def test_concurrent_claims_get_distinct_rows():
    from src.models.user import User

    warm_keys = {await _add_pool_key(age_seconds=120), await _add_pool_key(age_seconds=120)}
    async with AsyncSessionLocal() as db:
        user = User(email=f"pool-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.commit()
        user_id = user.id

    async def _claim_one(name: str) -> str | None:
        async with AsyncSessionLocal() as db:
            row = await ApiKeyPoolService.claim_warm_key(
                db, target_type=ApiKeyType.api, user_id=user_id, name=name
            )
            await db.commit()
            return row.key if row else None

    results = await asyncio.gather(_claim_one("a"), _claim_one("b"))
    assert None not in results
    assert set(results) == warm_keys  # two distinct warm rows, no double-claim

    # cleanup the two claimed keys + user
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        from src.models.user import User as U

        await db.execute(delete(U).where(U.id == user_id))
        await db.commit()


async def test_claim_warm_string_returns_key_and_removes_pool_row():
    warm = await _add_pool_key(age_seconds=120)
    async with AsyncSessionLocal() as db:
        s = await ApiKeyPoolService.claim_warm_string(db)
        await db.commit()
        assert s == warm
    # the pool row is gone (consumed), so the pool is now empty
    assert await _pool_count() == 0


async def test_claim_warm_string_returns_none_when_no_ready_key():
    await _add_pool_key(age_seconds=0)  # too fresh
    async with AsyncSessionLocal() as db:
        s = await ApiKeyPoolService.claim_warm_string(db)
        await db.rollback()
        assert s is None


async def test_schedule_refill_tops_up(monkeypatch):
    from src.config import config

    monkeypatch.setattr(config, "POOL_SIZE", 2)
    task = ApiKeyPoolService.schedule_refill()
    await task
    assert await _pool_count() == 2
