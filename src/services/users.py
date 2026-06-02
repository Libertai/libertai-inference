import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user import User
from src.models.wallet_connection import WalletConnection


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


def infer_chain(address: str) -> str:
    """Infer the chain from an address shape (matches the migration backfill rule)."""
    return "base" if address.startswith("0x") else "solana"


async def get_or_create_user_by_wallet(db: AsyncSession, address: str, chain: str | None = None) -> User:
    """Resolve a wallet address to its user, creating the user + wallet link if needed.

    Used by the on-chain credit watchers and API-key creation, which only know an address.
    The session is flushed (not committed) so the caller controls the transaction.
    """
    chain = chain or infer_chain(address)

    wallet = (
        await db.execute(select(WalletConnection).where(WalletConnection.address == address))
    ).scalars().first()
    if wallet is not None:
        user = await db.get(User, wallet.user_id)
        if user is not None:
            return user

    # Legacy fallback: a user row may still carry the address directly (pre-backfill edge case).
    user = (await db.execute(select(User).where(User.address == address))).scalars().first()
    if user is None:
        user = User(address=address)
        db.add(user)
        await db.flush()

    if wallet is None:
        db.add(WalletConnection(user_id=user.id, chain=chain, address=address, is_primary=True))
        await db.flush()

    return user
