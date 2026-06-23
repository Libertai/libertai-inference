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
from src.models.wallet_connection import WalletConnection
from src.services.auth_tokens import create_access_token
from src.services.credit import CreditService
from src.services.payments.base import CheckoutResult
from src.services.payments.registry import payment_registry
from src.topup_packs import TOPUP_PACKS


async def _auth_user() -> tuple[User, dict]:
    """Email/OAuth user with no wallet connection (fiat rail)."""
    async with AsyncSessionLocal() as db:
        user = User(email=f"pay-route-{int(time.time()*1000)}@example.com", email_verified=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


async def _wallet_user(chain: str = "base") -> tuple[User, dict]:
    """Wallet user (has a WalletConnection — on-chain rail)."""
    user, headers = await _auth_user()
    async with AsyncSessionLocal() as db:
        db.add(WalletConnection(user_id=user.id, chain=chain, address=f"0xroute{user.id.hex}", is_primary=True))
        await db.commit()
    return user, headers


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
    assert {"free", "go", "plus", "max"} == names


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


async def test_subscription_window_resets_at_serialized_as_utc(async_client):
    """Reset timestamps must carry a UTC offset, not a naive offset-less string.

    The columns are naive UTC; serialized without an offset, JS ``new Date`` reads them
    as browser-local and the reset countdown skews by the client's UTC offset (showed
    "Resets now" on a still-live window). Regression guard for that.
    """
    from datetime import datetime

    from src.services.entitlement import open_windows

    user, headers = await _auth_user()
    try:
        async with AsyncSessionLocal() as db:
            await open_windows(db, user.id)
            await db.commit()
        resp = await async_client.get("/payments/subscription", headers=headers)
        assert resp.status_code == 200
        resets_at = resp.json()["window_5h_resets_at"]
        assert resets_at is not None
        # Parseable as an aware instant (no offset would make fromisoformat tz-naive).
        assert datetime.fromisoformat(resets_at).tzinfo is not None
        assert resets_at.endswith("+00:00")
    finally:
        async with AsyncSessionLocal() as db:
            from src.models.entitlement_window import EntitlementWindow

            await db.execute(delete(EntitlementWindow).where(EntitlementWindow.user_id == user.id))
            await db.commit()
        await _cleanup(user.id)


async def test_topup_then_webhook_credits_user(async_client, monkeypatch):
    user, headers = await _auth_user()
    revolut = payment_registry.get("revolut")
    # Enable the provider + stub the outbound checkout call (no real Revolut HTTP).
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")

    async def fake_create_topup(
        *, amount, currency, redirect_url, user_email=None, metadata=None, vat_rate=0.0, item_name="Prepaid credits"
    ):
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

    async def fake_create_topup(
        *, amount, currency, redirect_url, user_email=None, metadata=None, vat_rate=0.0, item_name="Prepaid credits"
    ):
        seen["calls"] += 1
        seen["redirect_url"] = redirect_url
        # external_reference is unique in DB -> each fake order needs a distinct id.
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


def _stub_topup_provider(monkeypatch, seen: dict, order_prefix: str):
    """Enable Revolut + capture create_topup kwargs (no real HTTP)."""
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")

    async def fake_create_topup(
        *, amount, currency, redirect_url, user_email=None, metadata=None, vat_rate=0.0, item_name="Prepaid credits"
    ):
        seen["calls"] = seen.get("calls", 0) + 1
        seen["amount"] = amount
        seen["currency"] = currency
        seen["vat_rate"] = vat_rate
        # external_reference is unique in DB -> each fake order needs a distinct id.
        return CheckoutResult(checkout_url="http://pay/checkout", order_id=f"{order_prefix}_{seen['calls']}")

    monkeypatch.setattr(revolut, "create_topup", fake_create_topup)


async def test_topup_eur_region_charges_pack_eur_credits_pack_usd(async_client, monkeypatch):
    """EU callers buy a fixed pack: the provider is charged the gross EUR, the pending row records USD credits."""
    user, headers = await _auth_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "EUR")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_eur_pack_{user.id}")
    pack = TOPUP_PACKS["eur_10"]

    try:
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "pack_id": "eur_10"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert seen["amount"] == pack.eur_charge
        assert seen["currency"] == "EUR"

        async with AsyncSessionLocal() as db:
            tx = (
                await db.execute(
                    select(CreditTransaction).where(CreditTransaction.user_id == user.id)
                )
            ).scalar_one()
        assert tx.status == CreditTransactionStatus.pending
        assert float(tx.amount) == pack.usd_credits
        assert float(tx.amount_left) == pack.usd_credits
    finally:
        await _cleanup(user.id)


async def test_topup_eur_region_without_pack_id_rejected(async_client, monkeypatch):
    user, headers = await _auth_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "EUR")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_eur_nopack_{user.id}")

    try:
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "amount": 20}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert "pack" in resp.json()["detail"].lower()
        assert seen.get("calls", 0) == 0
    finally:
        await _cleanup(user.id)


async def test_topup_eur_region_unknown_pack_rejected(async_client, monkeypatch):
    user, headers = await _auth_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "EUR")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_eur_badpack_{user.id}")

    try:
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "pack_id": "nope"}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert seen.get("calls", 0) == 0
    finally:
        await _cleanup(user.id)


async def test_topup_eur_pack_passes_vat_rate(async_client, monkeypatch):
    """EU pack checkout passes the 20% VAT rate to the provider (broken out on the invoice);
    USD top-ups pass 0."""
    user, headers = await _auth_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "EUR")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_eur_vat_{user.id}")

    try:
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "pack_id": "eur_10"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert seen["vat_rate"] == 0.20
    finally:
        await _cleanup(user.id)


async def test_topup_usd_region_charges_arbitrary_amount_one_to_one(async_client, monkeypatch):
    user, headers = await _auth_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "USD")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_usd_{user.id}")

    try:
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "amount": 20}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert seen["amount"] == 20.0
        assert seen["currency"] == "USD"

        async with AsyncSessionLocal() as db:
            tx = (
                await db.execute(
                    select(CreditTransaction).where(CreditTransaction.user_id == user.id)
                )
            ).scalar_one()
        assert float(tx.amount) == 20.0
    finally:
        await _cleanup(user.id)


async def test_topup_usd_region_without_amount_rejected(async_client, monkeypatch):
    user, headers = await _auth_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "USD")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_usd_noamt_{user.id}")

    try:
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "pack_id": "eur_10"}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert seen.get("calls", 0) == 0
    finally:
        await _cleanup(user.id)


async def test_topup_usd_region_with_pack_id_rejected_even_with_amount(async_client, monkeypatch):
    """USD mirrors EUR strictness: a stray pack_id is rejected, not silently ignored."""
    user, headers = await _auth_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "USD")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_usd_both_{user.id}")

    try:
        resp = await async_client.post(
            "/payments/topup",
            json={"provider": "revolut", "amount": 20, "pack_id": "eur_10"},
            headers=headers,
        )
        assert resp.status_code == 400, resp.text
        assert "pack_id" in resp.json()["detail"]
        assert seen.get("calls", 0) == 0
    finally:
        await _cleanup(user.id)


async def test_topup_amount_capped_at_10k(async_client, monkeypatch):
    """Sanity ceiling on arbitrary USD amounts: 10000 passes validation, 10001 is a 422."""
    user, headers = await _auth_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "USD")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_usd_cap_{user.id}")

    try:
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "amount": 10_001}, headers=headers
        )
        assert resp.status_code == 422, resp.text
        assert seen.get("calls", 0) == 0

        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "amount": 10_000}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert seen["amount"] == 10_000.0
        assert seen["currency"] == "USD"
    finally:
        await _cleanup(user.id)


async def test_topup_packs_endpoint_lists_packs(async_client):
    resp = await async_client.get("/payments/topup-packs")
    assert resp.status_code == 200
    packs = resp.json()
    assert len(packs) == 4
    assert {p["id"] for p in packs} == set(TOPUP_PACKS)
    for p in packs:
        assert p["usd_credits"] > 0
        assert p["eur_charge"] > 0


async def test_subscribe_uses_region_resolved_currency(async_client, monkeypatch):
    """/subscribe resolves the caller's currency from their IP and passes it to the provider."""
    user, headers = await _auth_user()
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "EUR")

    seen: dict = {}

    async def fake_create_subscription(*, user_email, tier, currency, redirect_url, provider_customer_id=None):
        seen["currency"] = currency
        return CheckoutResult(
            checkout_url="http://pay/sub",
            provider_subscription_id=f"psub_route_{user.id}",
            provider_customer_id="cust_route",
            order_id=f"setup_route_{user.id}",
        )

    monkeypatch.setattr(revolut, "create_subscription", fake_create_subscription)

    try:
        resp = await async_client.post(
            "/payments/subscribe", json={"provider": "revolut", "tier": "go"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert seen["currency"] == "EUR"
        async with AsyncSessionLocal() as db:
            sub = (await db.execute(select(PlanSubscription).where(PlanSubscription.user_id == user.id))).scalar_one()
        assert sub.currency == "EUR"
    finally:
        await _cleanup(user.id)


async def test_upgrade_uses_region_resolved_currency(async_client, monkeypatch):
    user, headers = await _auth_user()
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "EUR")

    seen: dict = {}

    async def fake_create_subscription(*, user_email, tier, currency, redirect_url, provider_customer_id=None):
        seen["currency"] = currency
        return CheckoutResult(
            checkout_url="http://pay/sub",
            provider_subscription_id=f"psub_upg_{user.id}",
            provider_customer_id="cust_upg",
            order_id=f"setup_upg_{user.id}",
        )

    async def fake_cancel_subscription(provider_subscription_id):
        return None

    monkeypatch.setattr(revolut, "create_subscription", fake_create_subscription)
    monkeypatch.setattr(revolut, "cancel_subscription", fake_cancel_subscription)

    try:
        # Seed an active "go" sub so /upgrade has something to upgrade from.
        async with AsyncSessionLocal() as db:
            db.add(
                PlanSubscription(
                    user_id=user.id,
                    tier="go",
                    status="active",
                    provider="revolut",
                    provider_subscription_id=f"psub_old_{user.id}",
                    currency="USD",
                )
            )
            await db.commit()

        resp = await async_client.post(
            "/payments/upgrade", json={"provider": "revolut", "tier": "plus"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert seen["currency"] == "EUR"
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
    user, headers = await _wallet_user()
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
    user, headers = await _wallet_user()
    try:
        # No credits seeded — should return 400.
        resp = await async_client.post(
            "/payments/subscribe", json={"provider": "credits", "tier": "go"}, headers=headers
        )
        assert resp.status_code == 400
        assert "credits" in resp.json()["detail"].lower()
    finally:
        await _cleanup(user.id)


async def test_subscribe_credits_rejected_for_email_user(async_client):
    """Email/OAuth users have no wallet, so they can't pay subscriptions with credits."""
    user, headers = await _auth_user()
    try:
        await CreditService.add_credits_for_user(user.id, 50.0, CreditTransactionProvider.voucher)
        resp = await async_client.post(
            "/payments/subscribe", json={"provider": "credits", "tier": "go"}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert "wallet" in resp.json()["detail"].lower()
    finally:
        await _cleanup(user.id)


async def test_subscribe_revolut_rejected_for_wallet_user(async_client, monkeypatch):
    """Wallet users pay on-chain only — Revolut subscriptions are refused."""
    user, headers = await _wallet_user()
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")
    try:
        resp = await async_client.post(
            "/payments/subscribe", json={"provider": "revolut", "tier": "go"}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert "on-chain" in resp.json()["detail"].lower()
    finally:
        await _cleanup(user.id)


async def test_upgrade_credits_rejected_for_email_user(async_client):
    user, headers = await _auth_user()
    try:
        resp = await async_client.post(
            "/payments/upgrade", json={"provider": "credits", "tier": "plus"}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert "wallet" in resp.json()["detail"].lower()
    finally:
        await _cleanup(user.id)


async def test_upgrade_revolut_rejected_for_wallet_user(async_client, monkeypatch):
    """Wallet user with no active fiat subscription can't open a fiat upgrade."""
    user, headers = await _wallet_user()
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")
    try:
        resp = await async_client.post(
            "/payments/upgrade", json={"provider": "revolut", "tier": "plus"}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert "on-chain" in resp.json()["detail"].lower()
    finally:
        await _cleanup(user.id)


async def test_upgrade_revolut_allowed_for_wallet_user_with_active_revolut_sub(async_client, monkeypatch):
    """Email user who subscribed via Revolut then connected a wallet can still upgrade that sub."""
    user, headers = await _wallet_user()
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "USD")

    seen: dict = {}

    async def fake_create_subscription(*, user_email, tier, currency, redirect_url, provider_customer_id=None):
        seen["tier"] = tier
        return CheckoutResult(
            checkout_url="http://pay/sub",
            provider_subscription_id=f"psub_wallet_upg_{user.id}",
            provider_customer_id="cust_wallet_upg",
            order_id=f"setup_wallet_upg_{user.id}",
        )

    async def fake_cancel_subscription(provider_subscription_id):
        return None

    monkeypatch.setattr(revolut, "create_subscription", fake_create_subscription)
    monkeypatch.setattr(revolut, "cancel_subscription", fake_cancel_subscription)

    try:
        # Subscribed via Revolut as an email user, then connected a wallet.
        async with AsyncSessionLocal() as db:
            db.add(
                PlanSubscription(
                    user_id=user.id,
                    tier="go",
                    status="active",
                    provider="revolut",
                    provider_subscription_id=f"psub_wallet_old_{user.id}",
                    currency="USD",
                )
            )
            await db.commit()

        resp = await async_client.post(
            "/payments/upgrade", json={"provider": "revolut", "tier": "plus"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert seen["tier"] == "plus"
    finally:
        await _cleanup(user.id)


async def test_topup_revolut_rejected_for_wallet_user(async_client, monkeypatch):
    """Wallet users top up via the on-chain contracts, not card checkout."""
    user, headers = await _wallet_user()
    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "USD")
    seen: dict = {}
    _stub_topup_provider(monkeypatch, seen, f"ord_wallet_{user.id}")
    try:
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "amount": 20}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert "on-chain" in resp.json()["detail"].lower()
        assert seen.get("calls", 0) == 0
    finally:
        await _cleanup(user.id)
