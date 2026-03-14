from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

from src.config import config

Base = declarative_base()

# Async engine + session for the app (psycopg v3)
_parsed_url = make_url(config.DATABASE_URL)
_async_url = _parsed_url.set(drivername="postgresql+psycopg")
async_engine = create_async_engine(_async_url, pool_size=20, max_overflow=5, pool_timeout=10, pool_recycle=1800)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
