import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

scheduler = AsyncIOScheduler()

ltai_payments_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown()
