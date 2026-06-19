import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.oauth_connection import OAuthConnection
from src.models.user import User
from src.models.wallet_connection import WalletConnection

if TYPE_CHECKING:
    from src.services.oauth import OAuthUserInfo


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


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Look up a user by email without creating one. Returns None if no account exists."""
    return (await db.execute(select(User).where(User.email == email.strip().lower()))).scalars().first()


async def get_or_create_user_by_email(db: AsyncSession, email: str) -> tuple[User, bool]:
    """Resolve an email to its user (created via magic link => email is verified). No wallet."""
    email = email.strip().lower()
    user = (await db.execute(select(User).where(User.email == email))).scalars().first()
    if user is not None:
        return user, False
    user = User(email=email, email_verified=True)
    db.add(user)
    await db.flush()
    return user, True


async def get_or_create_user_by_oauth(db: AsyncSession, info: "OAuthUserInfo") -> tuple[User, bool]:
    """Resolve an OAuth identity to its user. Links to an existing email account if one matches.

    Email/OAuth users never get a wallet. Returns (user, created).
    """
    existing = (
        await db.execute(
            select(OAuthConnection).where(
                OAuthConnection.provider == info.provider, OAuthConnection.provider_id == info.provider_id
            )
        )
    ).scalars().first()
    if existing is not None:
        user = await db.get(User, existing.user_id)
        if user is not None:
            return user, False

    user = None
    if info.email:
        user = (await db.execute(select(User).where(User.email == info.email.strip().lower()))).scalars().first()

    created = False
    if user is None:
        user = User(
            email=info.email.strip().lower() if info.email else None,
            email_verified=info.email_verified,
            display_name=info.name,
            avatar_url=info.avatar_url,
        )
        db.add(user)
        await db.flush()
        created = True

    await link_oauth(db, user, info)
    return user, created


async def link_oauth(db: AsyncSession, user: User, info: "OAuthUserInfo") -> None:
    """Attach an OAuth identity to a user (no-op if already linked)."""
    existing = (
        await db.execute(
            select(OAuthConnection).where(
                OAuthConnection.provider == info.provider, OAuthConnection.provider_id == info.provider_id
            )
        )
    ).scalars().first()
    if existing is not None:
        return
    db.add(
        OAuthConnection(
            user_id=user.id, provider=info.provider, provider_id=info.provider_id, provider_email=info.email
        )
    )
    await db.flush()


async def link_wallet(db: AsyncSession, user: User, address: str, chain: str | None = None) -> WalletConnection:
    """Attach a wallet to a user (used when a fiat user later connects crypto)."""
    chain = chain or infer_chain(address)
    existing = (
        await db.execute(select(WalletConnection).where(WalletConnection.address == address))
    ).scalars().first()
    if existing is not None:
        return existing
    has_primary = (
        await db.execute(select(WalletConnection).where(WalletConnection.user_id == user.id))
    ).scalars().first() is not None
    wallet = WalletConnection(user_id=user.id, chain=chain, address=address, is_primary=not has_primary)
    db.add(wallet)
    await db.flush()
    return wallet


async def update_user_profile(db: AsyncSession, user_id: uuid.UUID, display_name: str | None) -> User:
    """Update the user's editable profile fields (currently just the display name)."""
    user = await db.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")
    user.display_name = display_name
    await db.flush()
    return user
