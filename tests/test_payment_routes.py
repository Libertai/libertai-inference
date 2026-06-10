"""Payment route smoke tests, including a signed end-to-end top-up webhook."""

import hashlib
import hmac
import json
import time

from sqlalchemy import delete, func, select

from src.config import config
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.services.auth_tokens import create_access_token
from src.services.credit import CreditService
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
        # PlanSubscriptionEvent rows cascade-delete with PlanSubscription.
        await db.execute(delete(PlanSubscription).where(PlanSubscription.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_tiers_endpoint(async_client):
    resp = await async_client.get("/payments/tiers")
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()}
    assert {"free", "go", "plus", "power"} == names


async def test_region_eu_ip_returns_eur_with_vat(async_client, monkeypatch):
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "EUR")
    resp = await async_client.get("/payments/region")
    assert resp.status_code == 200
    assert resp.json() == {"currency": "EUR", "vat_rate": 0.20}


async def test_region_non_eu_ip_returns_usd_no_vat(async_client, monkeypatch):
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "USD")
    resp = await async_client.get("/payments/region")
    assert resp.status_code == 200
    assert resp.json() == {"currency": "USD", "vat_rate": 0.0}


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


async def test_topup_redirects_back_to_requesting_app(async_client, monkeypatch):
    """An allowlisted redirect_base (chat vs console) decides where checkout returns the user."""
    user, headers = await _auth_user()
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")
    monkeypatch.setattr(config, "ALLOWED_FRONTEND_URLS", ["https://chat.libertai.io", "https://console.libertai.io"])
    monkeypatch.setattr(config, "FRONTEND_URL", "https://console.libertai.io")

    seen: dict = {"calls": 0}

    async def fake_create_topup(*, amount, currency, redirect_url, user_email=None, metadata=None):
        seen["calls"] += 1
        seen["redirect_url"] = redirect_url
        # transaction_hash is unique in DB -> each fake order needs a distinct id.
        return CheckoutResult(checkout_url="http://pay/checkout", order_id=f"ord_redirect_{user.id}_{seen['calls']}")

    monkeypatch.setattr(revolut, "create_topup", fake_create_topup)

    try:
        # Allowed origin -> checkout returns to that app's callback page.
        resp = await async_client.post(
            "/payments/topup",
            json={"provider": "revolut", "amount": 5, "redirect_base": "https://chat.libertai.io"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert seen["redirect_url"] == "https://chat.libertai.io/payment/callback"

        # Disallowed origin -> fall back to the default frontend.
        resp = await async_client.post(
            "/payments/topup",
            json={"provider": "revolut", "amount": 5, "redirect_base": "https://evil.example.com"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert seen["redirect_url"] == "https://console.libertai.io/payment/callback"

        # No redirect_base -> same fallback.
        resp = await async_client.post("/payments/topup", json={"provider": "revolut", "amount": 5}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert seen["redirect_url"] == "https://console.libertai.io/payment/callback"
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


async def test_subscription_exposes_allowed_and_source(async_client):
    user, headers = await _auth_user()
    try:
        resp = await async_client.get("/payments/subscription", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "free"
        assert body["allowed"] is True
        assert body["source"] == "tier"
    finally:
        await _cleanup(user.id)


async def test_credits_subscribe_success(async_client):
    user, headers = await _auth_user()
    try:
        # Seed enough credits for a "go" subscription.
        await CreditService.add_credits_for_user(user.id, 50.0, CreditTransactionProvider.voucher)

        resp = await async_client.post(
            "/payments/subscribe", json={"provider": "credits", "tier": "go"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["checkout_url"] is None

        sub_resp = await async_client.get("/payments/subscription", headers=headers)
        assert sub_resp.status_code == 200
        body = sub_resp.json()
        assert body["tier"] == "go"
        assert body["has_subscription"] is True
        assert body["status"] == "active"
    finally:
        await _cleanup(user.id)


async def test_credits_subscribe_insufficient(async_client):
    user, headers = await _auth_user()
    try:
        # No credits seeded — should return 400.
        resp = await async_client.post(
            "/payments/subscribe", json={"provider": "credits", "tier": "go"}, headers=headers
        )
        assert resp.status_code == 400
        assert "credits" in resp.json()["detail"].lower()
    finally:
        await _cleanup(user.id)
