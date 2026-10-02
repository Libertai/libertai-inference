"""Aleph Cloud credit transfers from the promo wallet.

An ``aleph_credit_transfer`` POST on channel ``ALEPH_CREDIT`` moves credits from the sender's own
balance (pyaleph rejects it if the balance is short), so the promo wallet's pre-funded pool caps
what campaigns can hand out on Aleph. The sender must not be one of pyaleph's whitelisted credit
addresses. Each recipient's lot expires at min(requested expiry, the funding lot's expiry), so fund
the promo wallet with credits that outlive the campaign.

Messages are signed here with eth-account rather than through the Aleph SDK: the format is small and
stable, and the caller stores the signed message so a retry resubmits the same item_hash.
"""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from solders.pubkey import Pubkey
from web3 import Web3

from src.config import config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

ALEPH_CREDIT_CHANNEL = "ALEPH_CREDIT"
ALEPH_CREDIT_TRANSFER_TYPE = "aleph_credit_transfer"
# Aleph Cloud's internal credit unit (front-backoffice src/lib/credits.ts).
ALEPH_CREDITS_PER_USD = 1_000_000

AlephMessageStatus = Literal["processed", "pending", "rejected", "unknown"]


def normalize_aleph_address(address: str) -> str | None:
    """The address as Aleph keys credit balances, or None if it's neither EVM nor Solana.

    Balances are matched on the exact string, so EVM addresses must be EIP-55 checksummed (what
    app.aleph.cloud queries with); a lowercased address would hold credits nobody can see.
    """
    address = address.strip()
    if address.startswith(("0x", "0X")):
        try:
            return Web3.to_checksum_address(address)
        except (ValueError, TypeError):
            return None
    try:
        Pubkey.from_string(address)
    except Exception:
        return None
    return address


def usd_to_aleph_credits(amount_usd: float) -> int:
    return int((Decimal(str(amount_usd)) * ALEPH_CREDITS_PER_USD).to_integral_value())


def promo_wallet_address() -> str | None:
    if not config.ALEPH_PROMO_PRIVATE_KEY:
        return None
    return Account.from_key(config.ALEPH_PROMO_PRIVATE_KEY).address


def build_transfer_message(recipient: str, amount_usd: float, expires_at: datetime, tags: list[str]) -> dict[str, Any]:
    """Sign a one-recipient credit transfer from the promo wallet. ``expires_at`` is naive UTC."""
    account = Account.from_key(config.ALEPH_PROMO_PRIVATE_KEY)
    now = time.time()
    content = {
        "address": account.address,
        "time": now,
        "type": ALEPH_CREDIT_TRANSFER_TYPE,
        "content": {
            "tags": tags,
            "transfer": {
                "credits": [
                    {
                        "address": recipient,
                        "amount": usd_to_aleph_credits(amount_usd),
                        # pyaleph reads transfer expirations as epoch milliseconds.
                        "expiration": int(expires_at.replace(tzinfo=UTC).timestamp() * 1000),
                    }
                ]
            },
        },
    }
    item_content = json.dumps(content, separators=(",", ":"))
    item_hash = sha256(item_content.encode()).hexdigest()
    verification_buffer = f"ETH\n{account.address}\nPOST\n{item_hash}"
    signature = account.sign_message(encode_defunct(text=verification_buffer)).signature.to_0x_hex()
    return {
        "chain": "ETH",
        "sender": account.address,
        "type": "POST",
        "channel": ALEPH_CREDIT_CHANNEL,
        "time": now,
        "item_type": "inline",
        "item_content": item_content,
        "item_hash": item_hash,
        "signature": signature,
    }


async def submit_message(message: dict[str, Any]) -> AlephMessageStatus:
    """Publish a signed message and wait for the node to process it. Raises on transport errors."""
    async with httpx.AsyncClient(base_url=config.ALEPH_API_URL, timeout=30) as client:
        response = await client.post("/api/v0/messages", json={"sync": True, "message": message})
    if response.status_code >= 500:
        response.raise_for_status()
    status = response.json().get("message_status")
    if status in ("processed", "pending", "rejected"):
        return status
    logger.warning(f"Unexpected Aleph publish response {response.status_code}: {response.text[:500]}")
    return "unknown"


async def get_message_status(item_hash: str) -> AlephMessageStatus:
    """Where an already-submitted message stands; "unknown" if the node has never seen it."""
    async with httpx.AsyncClient(base_url=config.ALEPH_API_URL, timeout=30) as client:
        response = await client.get(f"/api/v0/messages/{item_hash}")
    if response.status_code == 404:
        return "unknown"
    response.raise_for_status()
    status = response.json().get("status")
    if status in ("processed", "pending", "rejected"):
        return status
    # removing/removed/forgotten: it was processed once, the credits were granted.
    return "processed" if status else "unknown"
