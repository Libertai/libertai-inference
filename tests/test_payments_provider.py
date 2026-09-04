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
async def test_create_subscription_resolves_liberclaw_plan_registry():
    """``tier`` names collide across products (e.g. "starter" is liberclaw-only); the plan
    lookup must resolve in the caller's product registry, not always the libertai one."""
    provider = _provider()
    provider._client = _FakeClient()

    result = await provider.create_subscription(
        user_email="a@b.c",
        tier="starter",
        currency="EUR",
        redirect_url="https://app/payment/callback",
        product="liberclaw",
    )
    assert result.checkout_url == "https://pay/x"


def test_get_provider_plan_defaults_to_libertai_registry():
    """Baseline for the test above: without a product, a liberclaw-only tier is invisible."""
    from src.subscription_tiers import get_provider_plan

    with pytest.raises(ValueError, match="Unknown tier: starter"):
        get_provider_plan("starter", "revolut", "EUR")


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
    registry.register(RevolutProvider(secret_key="", webhook_secret="", api_url="https://x", api_version="v"))
    registry.register(ThirdwebPaymentProvider(contract_address=None))
    assert registry.available_for_chains([]) == []
    assert registry.available_for_chains(["base"]) == []


def test_revolut_subscription_event_carries_subscription_id():
    body = json.dumps({"event": "SUBSCRIPTION_CANCELLED", "subscription_id": "sub_123"}).encode()
    ts = str(int(time.time() * 1000))
    event = _provider().parse_webhook(_sign(body, ts), body)
    assert event.type == PaymentEventType.subscription_cancelled
    assert event.provider_subscription_id == "sub_123"


def test_revolut_subscription_event_id_distinguishes_subscriptions():
    ts = str(int(time.time() * 1000))
    ids = []
    for sub_id in ("sub_a", "sub_b"):
        body = json.dumps({"event": "SUBSCRIPTION_OVERDUE", "subscription_id": sub_id}).encode()
        ids.append(_provider().parse_webhook(_sign(body, ts), body).provider_event_id)
    assert ids[0] != ids[1]


# --- rebuilding an activation the provider never delivered ---


class _ReadResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _ReadClient:
    """Serves the three reads the reconstruction makes, by URL."""

    is_closed = False  # the provider's client property checks this before reusing it

    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.requested: list[str] = []

    async def get(self, url: str) -> _ReadResponse:
        self.requested.append(url)
        return _ReadResponse(self.routes[url])


SUB_ID = "sub-1"
CYCLE_ID = "cycle-1"
ORDER_ID = "order-1"


def _wire(provider, *, state="active", cycle=None, order=None):
    routes = {
        f"/api/subscriptions/{SUB_ID}": {"id": SUB_ID, "state": state, "current_cycle_id": CYCLE_ID},
        f"/api/subscriptions/{SUB_ID}/cycles/{CYCLE_ID}": cycle if cycle is not None else {"order_id": ORDER_ID},
        f"/api/orders/{ORDER_ID}": order if order is not None else {"state": "completed", "type": "payment"},
    }
    provider._client = _ReadClient(routes)
    return provider


@pytest.mark.asyncio
async def test_missed_activation_event_is_keyed_like_a_real_delivery():
    """A late redelivery must dedup against it, so the id has to match parse_webhook's."""
    event = await _wire(_provider()).missed_activation_event(SUB_ID)
    assert event is not None
    assert event.type == PaymentEventType.order_completed
    assert event.provider_event_id == f"ORDER_COMPLETED:{ORDER_ID}"
    assert event.provider_subscription_id == SUB_ID
    assert event.order_id == ORDER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "cancelled", "finished"])
async def test_no_event_when_the_subscription_is_not_live(state):
    assert await _wire(_provider(), state=state).missed_activation_event(SUB_ID) is None


@pytest.mark.asyncio
async def test_no_event_when_the_cycle_names_no_order():
    assert await _wire(_provider(), cycle={}).missed_activation_event(SUB_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [
        {"state": "pending", "type": "payment"},
        # A refund settles as its own completed order; taken as a payment it would activate
        # a subscription on money that was handed back.
        {"state": "completed", "type": "refund"},
    ],
)
async def test_no_event_when_the_order_is_not_a_completed_payment(order):
    assert await _wire(_provider(), order=order).missed_activation_event(SUB_ID) is None


@pytest.mark.asyncio
async def test_a_non_subscription_provider_reports_it_cannot_reconcile():
    with pytest.raises(UnsupportedCapability):
        await SolanaPaymentProvider(contract_address=None).missed_activation_event("x")


# --- list_orders: single-page fetch, no working pagination on this API version ---


class _OrdersListResponse:
    def __init__(self, orders: list[dict]):
        self._orders = orders

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        # Mirrors the live endpoint's envelope: {"orders": [...]}.
        return {"orders": self._orders}


class _OrdersListClient:
    is_closed = False

    def __init__(self, orders: list[dict]):
        self.orders = orders
        self.requested_params: dict | None = None

    async def get(self, path: str, params: dict | None = None) -> _OrdersListResponse:
        assert path == "/api/orders"
        self.requested_params = params
        return _OrdersListResponse(self.orders)


@pytest.mark.asyncio
async def test_list_orders_returns_all_orders_under_the_cap():
    provider = _provider()
    client = _OrdersListClient([{"id": f"ord_{i}"} for i in range(3)])
    provider._client = client

    orders = await provider.list_orders()

    assert [o["id"] for o in orders] == ["ord_0", "ord_1", "ord_2"]
    assert client.requested_params == {"limit": 500}


@pytest.mark.asyncio
async def test_list_orders_raises_when_page_is_exactly_at_the_limit():
    """A page landing exactly at ``limit`` can't be told apart from a truncated one —
    this API version's pagination params are silently ignored, so it must never be
    treated as the complete ledger."""
    provider = _provider()
    client = _OrdersListClient([{"id": f"ord_{i}"} for i in range(5)])
    provider._client = client

    with pytest.raises(RuntimeError, match="may be truncated"):
        await provider.list_orders(limit=5)
