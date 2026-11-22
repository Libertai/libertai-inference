import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.services import x402 as x402_module
from src.services.x402 import X402Service

UPTO = {
    "scheme": "upto",
    "network": "eip155:8453",
    "amount": "20000",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x48E5A3e497D0C092D5B0Ba22B5A86ADA94227DFa",
    "maxTimeoutSeconds": 3600,
    "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "permit2", "facilitatorAddress": "0xFa"},
}
EXACT = {**UPTO, "scheme": "exact", "amount": "40000", "extra": {"name": "USD Coin", "version": "2"}}


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    monkeypatch.setattr(x402_module.config, "CDP_API_KEY_ID", "kid")
    monkeypatch.setattr(x402_module.config, "CDP_API_KEY_SECRET", pem)
    monkeypatch.setattr(x402_module.config, "X402_MIN_PRICE", 0.005)


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (0.012, "12000"),  # actual usage
        (0.0001, "5000"),  # floored at the minimum price
        (0.5, "20000"),  # capped at the authorized maximum
    ],
)
def test_upto_settles_actual_cost_within_bounds(actual, expected):
    assert X402Service.settlement_requirements(UPTO, actual)["amount"] == expected


def test_exact_settles_signed_amount():
    assert X402Service.settlement_requirements(EXACT, 0.001) == EXACT


def _capture_post(monkeypatch, status=200, body=None):
    calls = []

    async def _post(self, url, json=None, headers=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(status, json=body if body is not None else {"success": True, "transaction": "0xabc"})

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return calls


def _header(accepted):
    payment = {"x402Version": 2, "accepted": accepted, "payload": {"signature": "0x"}}
    return base64.b64encode(json.dumps(payment).encode()).decode()


@pytest.mark.asyncio
async def test_settle_posts_v2_body_to_cdp(monkeypatch):
    calls = _capture_post(monkeypatch)
    assert await X402Service.settle_payment(_header(UPTO), json.dumps(UPTO), 0.012)
    (call,) = calls
    assert call["url"] == "https://api.cdp.coinbase.com/platform/v2/x402/settle"
    assert call["headers"]["Authorization"].startswith("Bearer ")
    assert call["json"]["x402Version"] == 2
    assert call["json"]["paymentPayload"]["accepted"] == UPTO
    assert call["json"]["paymentRequirements"] == {**UPTO, "amount": "12000"}


@pytest.mark.asyncio
async def test_settle_reports_facilitator_failure(monkeypatch):
    errors: list[str] = []
    monkeypatch.setattr(x402_module.logger, "error", lambda msg, *args, **kwargs: errors.append(msg))
    _capture_post(monkeypatch, body={"success": False, "errorReason": "insufficient_funds"})
    payment = {"x402Version": 2, "accepted": UPTO, "payload": {"permit2Authorization": {"from": "0xPayer"}}}
    header = base64.b64encode(json.dumps(payment).encode()).decode()
    assert not await X402Service.settle_payment(header, json.dumps(UPTO), 0.012)
    (error,) = errors
    assert error.startswith("x402 settlement failed: payer=0xPayer scheme=upto amount=12000 status=200")


@pytest.mark.asyncio
async def test_settle_never_raises_on_garbage(monkeypatch):
    calls = _capture_post(monkeypatch)
    assert not await X402Service.settle_payment("not base64!", "{}", 0.01)
    assert not calls
