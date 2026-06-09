"""Payment route smoke tests, including a signed end-to-end top-up webhook."""

import hashlib
import hmac
import json
import time

from sqlalchemy import delete, func, select

from src.interfaces.credits import CreditTransactionStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
from src.services.auth_tokens import create_access_token
from src.services.payments.base import CheckoutResult
from src.services.payments.registry import payment_registry


async def _auth_user() -> tuple[User, dict]:
    async with AsyncSessionLocal() as db:
        user = User(email=f"pay-route-{int(time.time()*1000)}@example.com", email_verified=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_tiers_endpoint(async_client):
    resp = await async_client.get("/payments/tiers")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert {"free", "go", "plus"} == names


async def test_subscription_requires_auth(async_client):
    assert (await async_client.get("/payments/subscription")).status_code == 401


async def test_subscription_defaults_to_free(async_client):
    user, headers = await _auth_user()
    try:
        resp = await async_client.get("/payments/subscription", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "free"
        assert body["has_subscription"] is False
    finally:
        await _cleanup(user.id)


async def test_topup_then_webhook_credits_user(async_client, monkeypatch):
    user, headers = await _auth_user()
    revolut = payment_registry.get("revolut")
    # Enable the provider + stub the outbound checkout call (no real Revolut HTTP).
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")

    async def fake_create_topup(*, amount, currency, redirect_url, user_email=None, metadata=None):
        return CheckoutResult(checkout_url="http://pay/checkout", order_id="ord_route_1")

    monkeypatch.setattr(revolut, "create_topup", fake_create_topup)

    try:
        # 1. Open a top-up checkout -> records a pending credit transaction.
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "amount": 12.5}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["checkout_url"] == "http://pay/checkout"

        # 2. A signed ORDER_COMPLETED webhook completes it.
        body = json.dumps({"event": "ORDER_COMPLETED", "order_id": "ord_route_1"}).encode()
        ts = str(int(time.time() * 1000))
        sig = "v1=" + hmac.new(b"wsk_test", f"v1.{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
        wh = await async_client.post(
            "/payments/webhook/revolut",
            content=body,
            headers={"revolut-request-timestamp": ts, "revolut-signature": sig},
        )
        assert wh.status_code == 200, wh.text

        # 3. Balance reflects the completed top-up.
        async with AsyncSessionLocal() as db:
            balance = (
                await db.execute(
                    select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                        CreditTransaction.user_id == user.id,
                        CreditTransaction.status == CreditTransactionStatus.completed,
                    )
                )
            ).scalar()
        assert float(balance) == 12.5
    finally:
        await _cleanup(user.id)


async def test_webhook_bad_signature_rejected(async_client, monkeypatch):
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")
    body = json.dumps({"event": "ORDER_COMPLETED", "order_id": "x"}).encode()
    ts = str(int(time.time() * 1000))
    resp = await async_client.post(
        "/payments/webhook/revolut",
        content=body,
        headers={"revolut-request-timestamp": ts, "revolut-signature": "v1=deadbeef"},
    )
    assert resp.status_code == 401


async def test_unknown_provider_webhook_404(async_client):
    assert (await async_client.post("/payments/webhook/nope", content=b"{}")).status_code == 404
