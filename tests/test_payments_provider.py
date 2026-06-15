"""Provider abstraction + Revolut webhook verification + registry filtering."""

import hashlib
import hmac
import json
import time

import pytest

from src.services.payments.base import (
    PaymentCapability,
    PaymentEventType,
    PaymentProviderKind,
    UnsupportedCapability,
)
from src.services.payments.crypto import SolanaPaymentProvider, ThirdwebPaymentProvider
from src.services.payments.registry import PaymentRegistry
from src.services.payments.revolut import RevolutProvider

WEBHOOK_SECRET = "wsk_test_secret"


def _provider() -> RevolutProvider:
    return RevolutProvider(
        secret_key="sk_test",
        webhook_secret=WEBHOOK_SECRET,
        api_url="https://merchant.revolut.com",
        api_version="2024-09-01",
    )


def _sign(body: bytes, timestamp: str) -> dict:
    payload = f"v1.{timestamp}.{body.decode()}"
    sig = "v1=" + hmac.new(WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {"revolut-request-timestamp": timestamp, "revolut-signature": sig}


def test_revolut_descriptor_capabilities():
    desc = _provider().descriptor()
    assert desc.kind == PaymentProviderKind.fiat
    assert PaymentCapability.topup in desc.capabilities
    assert PaymentCapability.subscription in desc.capabilities
    assert desc.currencies == ["USD", "EUR"]


def test_revolut_webhook_valid_signature_parses_event():
    body = json.dumps({"event": "ORDER_COMPLETED", "order_id": "ord_123"}).encode()
    ts = str(int(time.time() * 1000))
    event = _provider().parse_webhook(_sign(body, ts), body)
    assert event.type == PaymentEventType.order_completed
    assert event.order_id == "ord_123"
    assert event.provider == "revolut"
    assert event.provider_event_id == "ORDER_COMPLETED:ord_123"


def test_revolut_webhook_tampered_body_fails():
    body = json.dumps({"event": "ORDER_COMPLETED", "order_id": "ord_123"}).encode()
    ts = str(int(time.time() * 1000))
    headers = _sign(body, ts)
    tampered = json.dumps({"event": "ORDER_COMPLETED", "order_id": "ord_evil"}).encode()
    with pytest.raises(ValueError, match="Invalid webhook signature"):
        _provider().parse_webhook(headers, tampered)


def test_revolut_webhook_stale_timestamp_fails():
    body = json.dumps({"event": "ORDER_COMPLETED", "order_id": "ord_123"}).encode()
    stale = str(int(time.time() * 1000) - 10 * 60 * 1000)  # 10 min ago
    with pytest.raises(ValueError, match="too old"):
        _provider().parse_webhook(_sign(body, stale), body)


def test_revolut_webhook_missing_headers_fails():
    body = b"{}"
    with pytest.raises(ValueError, match="Missing webhook signature"):
        _provider().parse_webhook({}, body)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


class _FakeClient:
    """Stub httpx client: a stale customer id 500s on subscription create; a fresh one works."""

    is_closed = False

    def __init__(self):
        self.created_customers = 0

    async def post(self, path: str, json: dict | None = None) -> _FakeResponse:
        if path == "/api/1.0/customers":
            self.created_customers += 1
            return _FakeResponse(200, {"id": "cust_fresh"})
        if path == "/api/subscriptions":
            if json["customer_id"] == "cust_stale":
                return _FakeResponse(500, {})
            return _FakeResponse(200, {"id": "sub_1", "setup_order_id": "ord_1"})
        raise AssertionError(f"unexpected POST {path}")

    async def get(self, path: str) -> _FakeResponse:
        assert path == "/api/orders/ord_1"
        return _FakeResponse(200, {"checkout_url": "https://pay/x"})


@pytest.mark.asyncio
async def test_create_subscription_retries_with_fresh_customer_when_reused_id_is_stale():
    """A reused customer id can be stale (deleted, or minted in another Revolut environment
    before a sandbox/production switch) — the checkout must heal by creating a fresh customer."""
    provider = _provider()
    provider._client = _FakeClient()

    result = await provider.create_subscription(
        user_email="a@b.c",
        tier="go",
        currency="USD",
        redirect_url="https://app/payment/callback",
        provider_customer_id="cust_stale",
    )
    assert result.provider_customer_id == "cust_fresh"
    assert result.checkout_url == "https://pay/x"
    assert provider._client.created_customers == 1


@pytest.mark.asyncio
async def test_create_subscription_does_not_retry_with_freshly_created_customer():
    """If the failure happens with a customer we JUST created, retrying won't help — propagate."""
    import httpx

    provider = _provider()
    client = _FakeClient()

    async def post(path: str, json: dict | None = None) -> _FakeResponse:
        if path == "/api/1.0/customers":
            client.created_customers += 1
            return _FakeResponse(200, {"id": "cust_stale"})  # fresh customer still 500s below
        return _FakeResponse(500, {})

    client.post = post
    provider._client = client

    with pytest.raises(httpx.HTTPStatusError):
        await provider.create_subscription(
            user_email="a@b.c",
            tier="go",
            currency="USD",
            redirect_url="https://app/payment/callback",
        )
    assert client.created_customers == 1  # no retry loop


class _CaptureClient:
    """Stub client that records the POST body and returns a ready order."""

    is_closed = False

    def __init__(self):
        self.body: dict | None = None

    async def post(self, path: str, json: dict | None = None) -> _FakeResponse:
        assert path == "/api/orders"
        self.body = json
        return _FakeResponse(200, {"id": "ord_x", "checkout_url": "https://pay/x"})


@pytest.mark.asyncio
async def test_create_topup_eur_breaks_out_inclusive_vat():
    """EUR top-up: the order amount stays the gross (VAT-inclusive) charge, and VAT is broken out
    within it as a line item (back-calculated), never added on top. €10.00 incl. 20% -> VAT €1.67."""
    provider = _provider()
    provider._client = _CaptureClient()

    result = await provider.create_topup(
        amount=10.0,
        currency="EUR",
        redirect_url="https://app/cb",
        vat_rate=0.20,
        item_name="LibertAI usage credits ($10)",
    )

    assert result.order_id == "ord_x"
    body = provider._client.body
    assert body["amount"] == 1000  # gross stays the authoritative charge (not 1200)
    item = body["line_items"][0]
    assert item["total_amount"] == 1000
    assert item["taxes"] == [{"name": "VAT 20%", "amount": 167}]  # 1000 - round(1000/1.2)=833


@pytest.mark.asyncio
async def test_create_topup_usd_has_a_line_item_but_no_tax():
    """USD top-up: a line item is still sent (Revolut risk-scrutinises orders without one) but it
    carries no VAT."""
    provider = _provider()
    provider._client = _CaptureClient()

    await provider.create_topup(amount=25.0, currency="USD", redirect_url="https://app/cb")

    body = provider._client.body
    assert body["amount"] == 2500
    item = body["line_items"][0]
    assert item["total_amount"] == 2500
    assert "taxes" not in item


def test_crypto_providers_topup_only_and_no_subscription():
    thirdweb = ThirdwebPaymentProvider(contract_address="0xabc")
    assert thirdweb.descriptor().chain == "base"
    assert thirdweb.supports(PaymentCapability.topup)
    assert not thirdweb.supports(PaymentCapability.subscription)


@pytest.mark.asyncio
async def test_crypto_provider_create_topup_unsupported():
    solana = SolanaPaymentProvider(contract_address="So111")
    with pytest.raises(UnsupportedCapability):
        await solana.create_topup(amount=5.0, currency="USDC", redirect_url="http://x")


def test_registry_available_for_chains_splits_fiat_and_crypto_by_wallet():
    registry = PaymentRegistry()
    registry.register(_provider())  # revolut, enabled (creds present)
    registry.register(ThirdwebPaymentProvider(contract_address="0xabc"))
    registry.register(SolanaPaymentProvider(contract_address="So111"))

    # Email-only user (no wallets): fiat only, never crypto.
    ids = [d.id for d in registry.available_for_chains([])]
    assert ids == ["revolut"]

    # EVM wallet user: on-chain only — thirdweb, no fiat, no solana.
    ids = {d.id for d in registry.available_for_chains(["base"])}
    assert ids == {"thirdweb"}

    # Solana wallet user: on-chain only.
    ids = {d.id for d in registry.available_for_chains(["solana"])}
    assert ids == {"solana"}


def test_registry_hides_disabled_providers():
    registry = PaymentRegistry()
    # No creds -> revolut disabled; crypto with no contract -> disabled.
    registry.register(
        RevolutProvider(secret_key="", webhook_secret="", api_url="https://x", api_version="v")
    )
    registry.register(ThirdwebPaymentProvider(contract_address=None))
    assert registry.available_for_chains([]) == []
    assert registry.available_for_chains(["base"]) == []
