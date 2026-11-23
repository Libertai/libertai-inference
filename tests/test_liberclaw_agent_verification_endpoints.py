"""Token-authed /liberclaw agent-verification endpoints (checkout + status)."""

import uuid

import httpx
import pytest
from sqlalchemy import delete, func, select

from src.config import config
from src.models.base import AsyncSessionLocal
from src.models.liberclaw_agent_verification import LiberclawAgentVerification
from src.models.liberclaw_user import LiberclawUser
from tests.test_liberclaw_invoice_endpoints import HEADERS, _install_fake_provider


@pytest.fixture(autouse=True)
def _liberclaw_secret(monkeypatch):
    monkeypatch.setattr(config, "LIBERCLAW_SECRET", HEADERS["x-liberclaw-token"])


async def _cleanup(account_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(LiberclawAgentVerification).where(LiberclawAgentVerification.liberclaw_account_id == account_id)
        )
        await db.execute(delete(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
        await db.commit()


async def _seed_account(account_id: uuid.UUID, *, status: str | None = None) -> str:
    email = f"{account_id.hex}@example.com"
    async with AsyncSessionLocal() as db:
        db.add(
            LiberclawUser(
                user_id=email,
                user_type="email",
                liberclaw_account_id=account_id,
            )
        )
        if status is not None:
            db.add(
                LiberclawAgentVerification(
                    liberclaw_account_id=account_id,
                    provider="revolut",
                    provider_order_id=f"ord-{account_id.hex}",
                    amount_cents=100,
                    currency="USD",
                    status=status,
                )
            )
        await db.commit()
    return email


def _checkout_body(account_id: uuid.UUID, email: str) -> dict:
    return {
        "liberclaw_account_id": str(account_id),
        "email": email,
        "redirect_url": "https://app.example/verification",
    }


@pytest.mark.asyncio
async def test_checkout_reports_already_verified_without_provider_call(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    email = await _seed_account(account_id, status="paid")
    try:
        resp = await async_client.post(
            "/liberclaw/agent-verification/checkout",
            headers=HEADERS,
            json=_checkout_body(account_id, email),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"url": None, "already_verified": True}
        assert fake.topups == []
    finally:
        await _cleanup(account_id)


@pytest.mark.asyncio
async def test_checkout_bridge_conflict_is_409_before_provider_call(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    owner_id = uuid.uuid4()
    caller_id = uuid.uuid4()
    email = f"{owner_id.hex}@example.com"
    async with AsyncSessionLocal() as db:
        db.add(LiberclawUser(user_id=email, user_type="email", liberclaw_account_id=owner_id))
        await db.commit()
    try:
        resp = await async_client.post(
            "/liberclaw/agent-verification/checkout",
            headers=HEADERS,
            json=_checkout_body(caller_id, email),
        )
        assert resp.status_code == 409
        assert fake.topups == []
    finally:
        await _cleanup(owner_id)
        await _cleanup(caller_id)


@pytest.mark.asyncio
async def test_status_reports_unverified_without_an_order(async_client):
    account_id = uuid.uuid4()
    await _seed_account(account_id)
    try:
        resp = await async_client.get(
            "/liberclaw/agent-verification",
            headers=HEADERS,
            params={"liberclaw_account_id": str(account_id)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "verified": False,
            "status": None,
            "order_id": None,
            "paid_at": None,
        }
    finally:
        await _cleanup(account_id)


@pytest.mark.asyncio
async def test_status_reports_paid_verification(async_client):
    account_id = uuid.uuid4()
    await _seed_account(account_id, status="paid")
    try:
        resp = await async_client.get(
            "/liberclaw/agent-verification",
            headers=HEADERS,
            params={"liberclaw_account_id": str(account_id)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["verified"] is True
        assert body["status"] == "paid"
        assert body["order_id"] == f"ord-{account_id.hex}"
    finally:
        await _cleanup(account_id)


@pytest.mark.asyncio
async def test_checkout_creates_order_and_returns_url(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    email = await _seed_account(account_id)
    try:
        resp = await async_client.post(
            "/liberclaw/agent-verification/checkout",
            headers=HEADERS,
            json=_checkout_body(account_id, email),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"url": "http://pay/topup", "already_verified": False}
        assert fake.topups == [(1.0, "USD")]
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(LiberclawAgentVerification).where(
                        LiberclawAgentVerification.liberclaw_account_id == account_id
                    )
                )
            ).scalar_one()
            assert row.status == "pending"
            assert row.amount_cents == 100
            assert row.currency == "USD"
            assert row.checkout_url == "http://pay/topup"
    finally:
        await _cleanup(account_id)


@pytest.mark.asyncio
async def test_checkout_provider_failure_returns_502_and_no_pending_row(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)

    async def _boom(**kwargs):
        raise httpx.HTTPError("provider down")

    monkeypatch.setattr(fake, "create_topup", _boom)
    account_id = uuid.uuid4()
    email = await _seed_account(account_id)
    try:
        resp = await async_client.post(
            "/liberclaw/agent-verification/checkout",
            headers=HEADERS,
            json=_checkout_body(account_id, email),
        )
        assert resp.status_code == 502
        async with AsyncSessionLocal() as db:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(LiberclawAgentVerification)
                    .where(LiberclawAgentVerification.liberclaw_account_id == account_id)
                )
            ).scalar_one()
            assert count == 0
    finally:
        await _cleanup(account_id)
