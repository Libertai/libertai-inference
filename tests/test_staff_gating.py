import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.auth_tokens import create_access_token

pytestmark = pytest.mark.asyncio


async def _token(staff: bool) -> str:
    async with AsyncSessionLocal() as db:
        user = User(email=f"gate-{'staff' if staff else 'user'}@example.com")
        user.is_libertai_staff = staff
        db.add(user)
        await db.commit()
        return create_access_token(user.id)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_global_stats_requires_auth(client):
    r = await client.get("/stats/global/summary?start_date=2026-01-01&end_date=2026-01-02")
    assert r.status_code == 401


async def test_global_stats_rejects_non_staff(client):
    token = await _token(staff=False)
    r = await client.get(
        "/stats/global/summary?start_date=2026-01-01&end_date=2026-01-02",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_global_stats_allows_staff(client):
    token = await _token(staff=True)
    r = await client.get(
        "/stats/global/summary?start_date=2026-01-01&end_date=2026-01-02",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


async def test_voucher_post_requires_staff(client):
    r = await client.post("/credits/vouchers", json={"amount": 5, "email": "nobody@example.com"})
    assert r.status_code == 401
