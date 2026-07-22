"""Advisory-lock single_runner: concurrent invocations don't overlap (replica-safety guard)."""

import asyncio

import pytest

from src.utils.pg_locks import single_runner

pytestmark = pytest.mark.asyncio

_TEST_LOCK_ID = 998877


async def test_concurrent_calls_skip_while_lock_held():
    started = asyncio.Event()
    release = asyncio.Event()

    @single_runner(_TEST_LOCK_ID, skip_result="skipped")
    async def job() -> str:
        started.set()
        await release.wait()
        return "ran"

    first = asyncio.create_task(job())
    await started.wait()
    # While the first call holds the lock, a second one skips instead of running.
    assert await job() == "skipped"
    release.set()
    assert await first == "ran"
    # Lock released: runs again normally.
    release.set()
    assert await job() == "ran"


async def test_skip_result_defaults_to_none():
    @single_runner(_TEST_LOCK_ID)
    async def job() -> str:
        return "ran"

    assert await job() == "ran"
