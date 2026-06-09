import asyncio

from sqlalchemy import select
from sqlalchemy.sql import func as sql_func

from src.config import config
from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Sentinel name for unclaimed pool rows. `api_keys.name` is NOT NULL, so pool rows
# need a placeholder; it is overwritten with the user's name on claim. Multiple pool
# rows sharing this name do not violate the legacy (user_address, name) unique
# constraint because user_address is NULL (NULLs are distinct in PG unique indexes).
POOL_SENTINEL_NAME = "__pool__"

# Serialize refills so two concurrent top-ups don't both observe a stale count and
# overshoot. A little overshoot is harmless, but the lock keeps the pool size exact.
_refill_lock = asyncio.Lock()

# Keep strong refs to fire-and-forget refill tasks so they aren't GC'd mid-flight.
_pending_refills: set[asyncio.Task] = set()


class ApiKeyPoolService:
    @staticmethod
    async def ensure_pool() -> int:
        """Top the pool back up to config.POOL_SIZE. Idempotent. Returns how many were created."""
        try:
            async with _refill_lock:
                async with AsyncSessionLocal() as db:
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
                        db.add(
                            ApiKeyDB(
                                key=ApiKeyDB.generate_key(),
                                name=POOL_SENTINEL_NAME,
                                type=ApiKeyType.pool,
                            )
                        )
                    await db.commit()
                    logger.info(f"Warm pool topped up by {deficit} (target {config.POOL_SIZE})")
                    return deficit
        except Exception as e:
            logger.error(f"Error ensuring warm API-key pool: {str(e)}", exc_info=True)
            return 0
