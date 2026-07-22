"""Liveness endpoint used by the orchestrator healthcheck."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_health(async_client):
    r = await async_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
