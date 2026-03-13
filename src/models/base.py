from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

from src.config import config

Base = declarative_base()

# Async engine + session for the app (psycopg v3)
_async_url = config.DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
async_engine = create_async_engine(_async_url, pool_size=20, max_overflow=5, pool_timeout=10, pool_recycle=1800)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
