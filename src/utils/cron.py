import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

scheduler = AsyncIOScheduler()

ltai_base_payments_lock = asyncio.Lock()
ltai_solana_payments_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from src.config import config
    from src.services.aleph import aleph_service
    from src.services.api_key_pool import ApiKeyPoolService
    from src.utils.token import close_async_client

    await aleph_service.fetch_models_data()
    # Seed the warm API-key pool so the first creations after boot are already propagated.
    await ApiKeyPoolService.ensure_pool()
    # Safety net: re-assert the pool size periodically in case a background refill failed.
    scheduler.add_job(
        ApiKeyPoolService.ensure_pool,
        "interval",
        seconds=config.POOL_RECONCILE_INTERVAL_SECONDS,
    )
    scheduler.start()
    yield
    scheduler.shutdown()
    await close_async_client()
