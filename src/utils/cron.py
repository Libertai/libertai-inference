import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

scheduler = AsyncIOScheduler()

ltai_base_payments_lock = asyncio.Lock()
ltai_solana_payments_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from src.services.aleph import aleph_service
    from src.utils.token import close_async_client

    await aleph_service.fetch_models_data()
    scheduler.start()
    yield
    scheduler.shutdown()
    await close_async_client()
