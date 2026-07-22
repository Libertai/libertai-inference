"""Cross-replica mutual exclusion via Postgres advisory locks.

The app runs as N identical replicas; in-process asyncio locks only serialize within one
replica. Anything that must run at most once across the fleet (cron ticks, migrations)
takes a session-level advisory lock in Postgres instead. Lock ids must be unique
app-wide — keep them all defined here.
"""

import functools
from typing import Any, Awaitable, Callable, TypeVar

from sqlalchemy import text

MIGRATIONS_LOCK_ID = 911000
POOL_RECONCILE_LOCK_ID = 911001
LTAI_BASE_LOCK_ID = 911002
LTAI_SOLANA_LOCK_ID = 911003

T = TypeVar("T")


def single_runner(lock_id: int, skip_result: Any = None) -> Callable:
    """Run the wrapped coroutine only if no other process/replica currently holds ``lock_id``;
    otherwise skip and return ``skip_result``. The lock is held on a dedicated connection for
    the duration of the call and auto-releases if that connection dies mid-run."""

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            from src.models.base import async_engine

            async with async_engine.connect() as conn:
                acquired = (
                    await conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id})
                ).scalar()
                if not acquired:
                    return skip_result
                try:
                    return await fn(*args, **kwargs)
                finally:
                    await conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})

        return wrapper

    return decorator
