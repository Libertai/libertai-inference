import uuid

import pytest

from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.auth_tokens import create_access_token

pytestmark = pytest.mark.asyncio

# A valid lowercase Base/EVM address (lowercase => no checksum requirement).
ADDR = "0x000000000000000000000000000000000000dead"


async def _token(staff: bool) -> str:
    async with AsyncSessionLocal() as db:
        user = User(email=f"gate-{'staff' if staff else 'user'}-{uuid.uuid4().hex}@example.com")
        user.is_libertai_staff = staff
        db.add(user)
        await db.commit()
        return create_access_token(user.id)


async def test_global_stats_requires_auth(async_client):
    r = await async_client.get("/stats/global/summary?start_date=2026-01-01&end_date=2026-01-02")
    assert r.status_code == 401


async def test_global_stats_rejects_non_staff(async_client):
    token = await _token(staff=False)
    r = await async_client.get(
        "/stats/global/summary?start_date=2026-01-01&end_date=2026-01-02",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_global_stats_allows_staff(async_client):
    token = await _token(staff=True)
    r = await async_client.get(
        "/stats/global/summary?start_date=2026-01-01&end_date=2026-01-02",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


async def test_voucher_post_requires_staff(async_client):
    r = await async_client.post("/credits/vouchers", json={"amount": 5, "email": "nobody@example.com"})
    assert r.status_code == 401


async def test_voucher_get_requires_auth(async_client):
    r = await async_client.get(f"/credits/vouchers?chain=base&address={ADDR}")
    assert r.status_code == 401


async def test_voucher_get_allows_staff(async_client):
    token = await _token(staff=True)
    r = await async_client.get(
        f"/credits/vouchers?chain=base&address={ADDR}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_voucher_expiration_requires_auth(async_client):
    r = await async_client.post(
        "/credits/voucher/expiration", json={"voucher_id": "not-a-real-uuid", "expired_at": None}
    )
    assert r.status_code == 401


async def test_voucher_expiration_allows_staff(async_client):
    token = await _token(staff=True)
    r = await async_client.post(
        "/credits/voucher/expiration",
        json={"voucher_id": "not-a-real-uuid", "expired_at": None},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Invalid (non-UUID) voucher_id is handled gracefully by the route, not a gating concern.
    assert r.status_code == 200
    assert r.json() is False
