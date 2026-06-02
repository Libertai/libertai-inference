"""Nonce-challenge wallet login for EVM addresses (eth_account signature recovery).

Solana wallets keep using the legacy /auth/login path; this is the new EVM path.
"""

import secrets
from datetime import datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.wallet_challenge import WalletChallenge

_CHALLENGE_TTL_MINUTES = 5


def _message_for_nonce(nonce: str) -> str:
    return f"Sign in to LibertAI.\n\nNonce: {nonce}"


async def create_challenge(db: AsyncSession, address: str) -> str:
    """Create a nonce challenge for an address and return the message the wallet must sign."""
    nonce = secrets.token_hex(16)
    db.add(
        WalletChallenge(
            address=address.lower(),
            nonce=nonce,
            expires_at=datetime.now() + timedelta(minutes=_CHALLENGE_TTL_MINUTES),
        )
    )
    await db.flush()
    return _message_for_nonce(nonce)


async def verify_signature(db: AsyncSession, address: str, signature: str) -> bool:
    """Verify a signature against the latest unexpired challenge for the address (single-use)."""
    now = datetime.now()
    challenge = (
        await db.execute(
            select(WalletChallenge)
            .where(WalletChallenge.address == address.lower())
            .order_by(WalletChallenge.created_at.desc())
        )
    ).scalars().first()
    if challenge is None or challenge.expires_at < now:
        return False

    try:
        recovered = Account.recover_message(encode_defunct(text=_message_for_nonce(challenge.nonce)), signature=signature)
    except Exception:
        return False

    await db.delete(challenge)
    await db.flush()
    return recovered.lower() == address.lower()
