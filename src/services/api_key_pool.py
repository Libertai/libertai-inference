import asyncio
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.sql import func as sql_func

from src.config import config
from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.utils.logger import setup_logger
from src.utils.pg_locks import POOL_RECONCILE_LOCK_ID, single_runner

logger = setup_logger(__name__)

# Sentinel name for unclaimed pool rows. `api_keys.name` is NOT NULL, so pool rows
# need a placeholder; it is overwritten with the user's name on claim.
POOL_SENTINEL_NAME = "__pool__"

# Serialize refills so two concurrent top-ups don't both observe a stale count and
# overshoot. A little overshoot is harmless, but the lock keeps the pool size exact.
_refill_lock = asyncio.Lock()

# Keep strong refs to fire-and-forget refill tasks so they aren't GC'd mid-flight.
_pending_refills: set[asyncio.Task] = set()


class ApiKeyPoolService:
    @staticmethod
    @single_runner(POOL_RECONCILE_LOCK_ID, skip_result=0)
    async def ensure_pool() -> int:
        """Top the pool back up to config.POOL_SIZE. Idempotent. Returns how many were created.

        Skips (returns 0) when another replica is already refilling — the count-then-create
        below would overshoot the pool if two processes ran it concurrently."""
        try:
            async with _refill_lock, AsyncSessionLocal() as db:
                count = int(
                    (
                        await db.execute(
                            select(sql_func.count())
                            .select_from(ApiKeyDB)
                            .where(ApiKeyDB.type == ApiKeyType.pool)
                        )
                    ).scalar()
                    or 0
                )
                deficit = config.POOL_SIZE - count
                if deficit <= 0:
                    return 0
                for _ in range(deficit):
                    row = ApiKeyDB(
                        key=ApiKeyDB.generate_key(),
                        name=POOL_SENTINEL_NAME,
                        type=ApiKeyType.pool,
                    )
                    row.created_at = datetime.now()
                    db.add(row)
                await db.commit()
                logger.info(f"Warm pool topped up by {deficit} (target {config.POOL_SIZE})")
                return deficit
        except Exception as e:
            logger.error(f"Error ensuring warm API-key pool: {e!s}", exc_info=True)
            return 0

    @staticmethod
    async def claim_warm_key(
        db,
        *,
        target_type: ApiKeyType,
        name: str,
        user_id: uuid.UUID | None = None,
        monthly_limit: float | None = None,
        user_address: str | None = None,
        expires_at: datetime | None = None,
        liberclaw_user_id: uuid.UUID | None = None,
    ) -> ApiKeyDB | None:
        """Atomically claim the oldest pool row that has been alive long enough to have
        propagated, repurposing it into the target key. Runs inside the caller's
        transaction (the caller commits). Returns the claimed row, or None if no warm
        pool key is ready (caller should fall back to cold generation).

        Concurrency: SELECT ... FOR UPDATE SKIP LOCKED guarantees two simultaneous
        claims never grab the same row. created_at is reset to now() so the
        user-facing creation time is correct; the warmth came from the key string
        already being in the whitelist, independent of created_at.
        """
        ready_before = datetime.now() - timedelta(seconds=config.POOL_WARM_THRESHOLD_SECONDS)
        stmt = (
            select(ApiKeyDB)
            .where(ApiKeyDB.type == ApiKeyType.pool, ApiKeyDB.created_at < ready_before)
            .order_by(ApiKeyDB.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = (await db.execute(stmt)).scalars().first()
        if row is None:
            return None

        row.type = target_type
        row.user_id = user_id
        row.name = name
        row.monthly_limit = monthly_limit
        row.user_address = user_address
        row.expires_at = expires_at
        row.liberclaw_user_id = liberclaw_user_id
        row.is_active = True
        row.deleted_at = None
        row.created_at = datetime.now()
        return row

    @staticmethod
    async def claim_warm_string(db) -> str | None:
        """Claim a warm pool key as a raw string and delete its pool row, for callers
        that must keep an existing row (CLI rotation adopts the warm string into the
        pre-existing CLI row to preserve usage history). Runs in the caller's
        transaction. The deleted pool row is flushed before the caller reassigns the
        string elsewhere, so the UNIQUE(key) constraint is never momentarily violated.
        """
        ready_before = datetime.now() - timedelta(seconds=config.POOL_WARM_THRESHOLD_SECONDS)
        stmt = (
            select(ApiKeyDB)
            .where(ApiKeyDB.type == ApiKeyType.pool, ApiKeyDB.created_at < ready_before)
            .order_by(ApiKeyDB.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        row = (await db.execute(stmt)).scalars().first()
        if row is None:
            return None
        key = row.key
        await db.delete(row)
        await db.flush()
        return key

    @staticmethod
    def schedule_refill() -> asyncio.Task:
        """Fire-and-forget refill of the pool back to POOL_SIZE. Returns the task so
        callers/tests can await it; production callers ignore the return value."""
        task = asyncio.create_task(ApiKeyPoolService.ensure_pool())
        _pending_refills.add(task)
        task.add_done_callback(_pending_refills.discard)
        return task
