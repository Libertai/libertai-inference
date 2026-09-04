from sqlalchemy import make_url
from sqlalchemy.event import listens_for
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.orm import declarative_base

from src.config import config

Base = declarative_base()

# Async engine + session for the app (psycopg v3)
_parsed_url = make_url(config.DATABASE_URL)
_async_url = _parsed_url.set(drivername="postgresql+psycopg")
async_engine = create_async_engine(_async_url, pool_size=20, max_overflow=5, pool_timeout=10, pool_recycle=1800)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


# LiberClaw snapshot push (src/services/payments/tier_push.py). Registered on the plain
# sync ``Session`` class (aliased ``SyncSession`` here — a ``Session`` model also exists
# under src/models/) rather than a subclass: every ``AsyncSession`` built by any
# ``async_sessionmaker`` that doesn't override ``sync_session_class`` (this one, and the
# test suite's own) is backed by an instance of exactly this class, so the hooks fire for
# all of them. Imports are deferred into the handler bodies — tier_push imports models
# that import this module, so importing it at module scope here would cycle.
@listens_for(SyncSession, "after_flush")
def _lclw_after_flush(session, flush_context) -> None:
    from src.services.payments.tier_push import collect_snapshot_pushes

    collect_snapshot_pushes(session)


@listens_for(SyncSession, "after_commit")
def _lclw_after_commit(session) -> None:
    from src.services.payments.tier_push import schedule_pending_pushes

    schedule_pending_pushes(session)


@listens_for(SyncSession, "after_rollback")
def _lclw_after_rollback(session) -> None:
    from src.services.payments.tier_push import clear_scheduled_pushes

    clear_scheduled_pushes(session)


@listens_for(SyncSession, "after_soft_rollback")
def _lclw_after_soft_rollback(session, previous_transaction) -> None:
    # A rolled-back SAVEPOINT (e.g. this repo's test fixture) doesn't fire ``after_rollback``
    # (that's only the outermost transaction) — this is the one that catches it, so a rolled
    # back change can never still fire a push on the session's next successful commit.
    from src.services.payments.tier_push import clear_scheduled_pushes

    clear_scheduled_pushes(session)
