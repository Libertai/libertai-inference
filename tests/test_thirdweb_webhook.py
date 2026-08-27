"""Thirdweb webhook route: payloads it must accept, and what it does with one it can't read.

Exercises the real handler against the committed test DB (services open their own
sessions), so each test cleans up its own rows.
"""

import hashlib
import hmac
import json
import time
import uuid

import pytest
from sqlalchemy import delete, select

from src.config import config
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.user import User

pytestmark = pytest.mark.asyncio

BASE_TX = "0xdf3e6fe914f9e73f47fa52b650b6cb601a3aa59caca582fb708b343b12b36b8b"
BSC_TX = "0xf8edb5799f56b8faf5fb31374a29a8c1be6f9bfd367fbf7bd5612d6701340686"
ONRAMP_ID = "onramp_5f1a2b3c4d5e"


def _signed_headers(body: str, legacy_names: bool = True) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        config.THIRDWEB_WEBHOOK_SECRET.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    names = ("X-Pay-Signature", "X-Pay-Timestamp") if legacy_names else ("X-Payload-Signature", "X-Timestamp")
    return {names[0]: signature, names[1]: timestamp, "Content-Type": "application/json"}


def _cross_chain_payload(user_id: uuid.UUID, status: str = "COMPLETED") -> dict:
    """A BSC -> Base purchase, with the token metadata and origin-side fields we never read."""
    return {
        "version": 2,
        "type": "pay.onchain-transaction",
        "data": {
            "transactionId": "0x59d5aab0a47a7cb18c6130e3540fdfed1b97740d9a3fd1d3b3fdd6331e2f82e2",
            "paymentId": "0xb7297c1a6d81e511aeb97b01ed9a8fe0f474b71066cce5ac94ffe2f203f45e85",
            "status": status,
            "originToken": {
                "chainId": 56,
                "address": "0x55d398326f99059fF775485246999027B3197955",
                "symbol": "USDT",
                "name": "Tether USD",
                "decimals": 18,
                "priceUsd": 0.9999914816706746,
                "volume24hUsd": 79842656319.92004,
            },
            "originAmount": "20045760320595593479",
            "destinationToken": {
                "chainId": 8453,
                "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "symbol": "USDC",
                "name": "USD Coin",
                "decimals": 6,
                "priceUsd": 0.9999992451642351,
            },
            "destinationAmount": "20000000",
            "sender": "0x1067b052c6772e8B00db25C7Ba52ae7Fe4d9f103",
            "receiver": str(config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE),
            "transactions": [
                {"chainId": 56, "transactionHash": BSC_TX},
                {"chainId": 8453, "transactionHash": BASE_TX},
            ],
            "developerFeeBps": 30,
            "developerFeeRecipient": "0x4988c9c03950f10b14bf7e8bee5d5a4ede8c45b7",
            "purchaseData": {"userId": str(user_id)},
        },
    }


def _onramp_payload(user_id: uuid.UUID, status: str = "COMPLETED") -> dict:
    """A card purchase settled onchain, keyed by the onramp id rather than a transaction hash."""
    return {
        "version": 2,
        "type": "pay.onramp-transaction",
        "data": {
            "id": ONRAMP_ID,
            "onramp": "stripe",
            "token": {
                "chainId": 8453,
                "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "symbol": "USDC",
                "name": "USD Coin",
                "decimals": 6,
                "priceUsd": 0.9999992451642351,
            },
            "amount": "20000000",
            "currency": "USD",
            "currencyAmount": 20.0,
            "receiver": str(config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE),
            "status": status,
            "purchaseData": {"userId": str(user_id)},
        },
    }


async def _create_user() -> User:
    async with AsyncSessionLocal() as db:
        user = User(address=f"0x{uuid.uuid4().hex[:40]}")
        db.add(user)
        await db.commit()
        return user


async def _cleanup(user_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _transactions(user_id: uuid.UUID) -> list[CreditTransaction]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CreditTransaction).where(CreditTransaction.user_id == user_id))
        return list(result.scalars().all())


async def test_cross_chain_purchase_credits_the_user(async_client):
    user = await _create_user()
    try:
        body = json.dumps(_cross_chain_payload(user.id))
        response = await async_client.post("/credits/thirdweb/webhook", content=body, headers=_signed_headers(body))

        assert response.status_code == 200
        transactions = await _transactions(user.id)
        assert len(transactions) == 1
        assert transactions[0].amount == pytest.approx(20.0)
        assert transactions[0].external_reference == BASE_TX
        assert transactions[0].status.value == "completed"
    finally:
        await _cleanup(user.id)


async def test_pending_then_completed_credits_once(async_client):
    user = await _create_user()
    try:
        for status in ("PENDING", "COMPLETED"):
            body = json.dumps(_cross_chain_payload(user.id, status=status))
            response = await async_client.post(
                "/credits/thirdweb/webhook", content=body, headers=_signed_headers(body)
            )
            assert response.status_code == 200

        transactions = await _transactions(user.id)
        assert len(transactions) == 1
        assert transactions[0].status.value == "completed"
    finally:
        await _cleanup(user.id)


async def test_failed_purchase_is_not_credited(async_client):
    user = await _create_user()
    try:
        body = json.dumps(_cross_chain_payload(user.id, status="FAILED"))
        response = await async_client.post("/credits/thirdweb/webhook", content=body, headers=_signed_headers(body))

        assert response.status_code == 200
        assert await _transactions(user.id) == []
    finally:
        await _cleanup(user.id)


async def test_unreadable_payload_is_rejected(async_client):
    payload = _cross_chain_payload(uuid.uuid4())
    del payload["data"]["purchaseData"]
    body = json.dumps(payload)

    response = await async_client.post("/credits/thirdweb/webhook", content=body, headers=_signed_headers(body))

    assert response.status_code == 400


async def test_bad_signature_is_rejected(async_client):
    body = json.dumps(_cross_chain_payload(uuid.uuid4()))
    headers = _signed_headers(body) | {"X-Pay-Signature": "00"}

    response = await async_client.post("/credits/thirdweb/webhook", content=body, headers=headers)

    assert response.status_code == 401


async def test_retired_v1_payload_is_ignored(async_client):
    body = json.dumps({"version": 1, "type": "pay.onchain-transaction", "data": {}})

    response = await async_client.post("/credits/thirdweb/webhook", content=body, headers=_signed_headers(body))

    assert response.status_code == 200


async def test_current_header_names_are_accepted(async_client):
    user = await _create_user()
    try:
        body = json.dumps(_cross_chain_payload(user.id))
        response = await async_client.post(
            "/credits/thirdweb/webhook", content=body, headers=_signed_headers(body, legacy_names=False)
        )

        assert response.status_code == 200
        assert len(await _transactions(user.id)) == 1
    finally:
        await _cleanup(user.id)


async def test_late_pending_event_leaves_a_completed_transaction_alone(async_client):
    user = await _create_user()
    try:
        for status in ("COMPLETED", "PENDING"):
            body = json.dumps(_cross_chain_payload(user.id, status=status))
            response = await async_client.post(
                "/credits/thirdweb/webhook", content=body, headers=_signed_headers(body)
            )
            assert response.status_code == 200

        transactions = await _transactions(user.id)
        assert len(transactions) == 1
        assert transactions[0].status.value == "completed"
    finally:
        await _cleanup(user.id)


async def test_repeated_pending_events_stay_pending(async_client):
    user = await _create_user()
    try:
        for _ in range(2):
            body = json.dumps(_cross_chain_payload(user.id, status="PENDING"))
            response = await async_client.post(
                "/credits/thirdweb/webhook", content=body, headers=_signed_headers(body)
            )
            assert response.status_code == 200

        transactions = await _transactions(user.id)
        assert len(transactions) == 1
        assert transactions[0].status.value == "pending"
    finally:
        await _cleanup(user.id)


async def test_onramp_purchase_credits_the_user(async_client):
    user = await _create_user()
    try:
        body = json.dumps(_onramp_payload(user.id))
        response = await async_client.post("/credits/thirdweb/webhook", content=body, headers=_signed_headers(body))

        assert response.status_code == 200
        transactions = await _transactions(user.id)
        assert len(transactions) == 1
        assert transactions[0].amount == pytest.approx(20.0)
        assert transactions[0].external_reference == ONRAMP_ID
        assert transactions[0].status.value == "completed"
    finally:
        await _cleanup(user.id)


async def test_onramp_pending_then_completed_credits_once(async_client):
    user = await _create_user()
    try:
        for status in ("PENDING", "COMPLETED"):
            body = json.dumps(_onramp_payload(user.id, status=status))
            response = await async_client.post(
                "/credits/thirdweb/webhook", content=body, headers=_signed_headers(body)
            )
            assert response.status_code == 200

        transactions = await _transactions(user.id)
        assert len(transactions) == 1
        assert transactions[0].status.value == "completed"
    finally:
        await _cleanup(user.id)


async def test_failed_onramp_purchase_is_not_credited(async_client):
    user = await _create_user()
    try:
        body = json.dumps(_onramp_payload(user.id, status="FAILED"))
        response = await async_client.post("/credits/thirdweb/webhook", content=body, headers=_signed_headers(body))

        assert response.status_code == 200
        assert await _transactions(user.id) == []
    finally:
        await _cleanup(user.id)
