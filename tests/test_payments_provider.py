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
