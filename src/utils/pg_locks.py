"""Cross-replica mutual exclusion via Postgres advisory locks.

The app runs as N identical replicas; in-process asyncio locks only serialize within one
replica. Anything that must run at most once across the fleet (cron ticks, migrations)
takes a session-level advisory lock in Postgres instead. Lock ids must be unique
app-wide — keep them all defined here.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy import text

MIGRATIONS_LOCK_ID = 911000
POOL_RECONCILE_LOCK_ID = 911001
LTAI_BASE_LOCK_ID = 911002
LTAI_SOLANA_LOCK_ID = 911003
LIFECYCLE_EMAILS_LOCK_ID = 911005

# Classid for the two-argument ``pg_advisory_*(classid, objid)`` form, whose keyspace is
# disjoint from the single-argument ids above. Its objid is derived per owner, so the ids it
# spans cannot be enumerated here (see ``PaymentManager._lock_owner``).
USER_SUBSCRIPTION_LOCK_CLASS = 911004

# Classid for invoice-number allocation; objid = invoice year. Held to end of
# transaction, so concurrent issuances serialize through commit and the second
# one sees the first's row (gap-free, no double allocation across replicas).
INVOICE_NUMBER_LOCK_CLASS = 911006

# LiberClaw snapshot-push retry drain (src/services/payments/tier_push.py): keeps two
# replicas from draining the same tier_push_pending markers concurrently.
LCLW_TIER_PUSH_DRAIN_LOCK_ID = 911007

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
                acquired = (await conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": lock_id})).scalar()
                if not acquired:
                    return skip_result
                try:
                    return await fn(*args, **kwargs)
                finally:
                    await conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})

        return wrapper

    return decorator
