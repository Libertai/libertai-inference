import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.oauth_connection import OAuthConnection
from src.models.user import User
from src.models.wallet_connection import WalletConnection
from src.services.disposable_email import DisposableEmailError, is_blocked_signup_domain, signup_domain
from src.utils.email_canonical import canonical_email, canonical_email_expression

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

    wallet = (await db.execute(select(WalletConnection).where(WalletConnection.address == address))).scalars().first()
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


def _by_canonical_email(email: str) -> Select[tuple[User]]:
    """Match on the expression the unique index is built on, so the lookup is an index scan."""
    return select(User).where(canonical_email_expression("users.email") == canonical_email(email))


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Look up a user by email without creating one. Returns None if no account exists."""
    return (await db.execute(_by_canonical_email(email))).scalars().first()


async def get_or_create_user_by_email(db: AsyncSession, email: str) -> tuple[User, bool]:
    """Resolve an email to its user, creating one if none exists. No wallet.

    Resolution is by canonical form, creation stores the address as typed.

    Raises ``DisposableEmailError`` rather than open an account on a blocked domain,
    whether the domain came from the static package list or ``blocked_email_domains``,
    or on an address that carries no domain at all.
    Only creation is gated: existing accounts keep authenticating whatever the lists say.
    """
    email = email.strip().lower()
    user = (await db.execute(_by_canonical_email(email))).scalars().first()
    if user is not None:
        return user, False
    if signup_domain(email) == "" or await is_blocked_signup_domain(db, email):
        raise DisposableEmailError(email)
    user = User(email=email)
    db.add(user)
    await db.flush()
    return user, True


async def get_or_create_user_by_oauth(db: AsyncSession, info: "OAuthUserInfo") -> tuple[User, bool]:
    """Resolve an OAuth identity to its user. Links to an existing email account if one matches.

    Email/OAuth users never get a wallet. Returns (user, created).
    """
    existing = (
        (
            await db.execute(
                select(OAuthConnection).where(
                    OAuthConnection.provider == info.provider, OAuthConnection.provider_id == info.provider_id
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        user = await db.get(User, existing.user_id)
        if user is not None:
            _refresh_avatar(user, info)
            return user, False

    user = None
    if info.email:
        user = (await db.execute(_by_canonical_email(info.email))).scalars().first()

    created = False
    if user is None:
        user = User(
            email=info.email.strip().lower() if info.email else None,
            display_name=info.name,
            avatar_url=info.avatar_url,
        )
        db.add(user)
        await db.flush()
        created = True
    else:
        _refresh_avatar(user, info)

    await link_oauth(db, user, info)
    return user, created


def _refresh_avatar(user: User, info: "OAuthUserInfo") -> None:
    """Sync the avatar from the provider. Not display_name — that one is user-editable."""
    if info.avatar_url and user.avatar_url != info.avatar_url:
        user.avatar_url = info.avatar_url


async def link_oauth(db: AsyncSession, user: User, info: "OAuthUserInfo") -> None:
    """Attach an OAuth identity to a user (no-op if already linked)."""
    existing = (
        (
            await db.execute(
                select(OAuthConnection).where(
                    OAuthConnection.provider == info.provider, OAuthConnection.provider_id == info.provider_id
                )
            )
        )
        .scalars()
        .first()
    )
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
        (await db.execute(select(WalletConnection).where(WalletConnection.address == address))).scalars().first()
    )
    if existing is not None:
        return existing
    has_primary = (
        await db.execute(select(WalletConnection).where(WalletConnection.user_id == user.id))
    ).scalars().first() is not None
    wallet = WalletConnection(user_id=user.id, chain=chain, address=address, is_primary=not has_primary)
    db.add(wallet)
    await db.flush()
    return wallet


async def update_user_profile(db: AsyncSession, user_id: uuid.UUID, updates: dict) -> User:
    """Partial update of the user's editable profile fields (only keys present are assigned)."""
    user = await db.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")
    for field in ("display_name", "monthly_extra_credit_cap"):
        if field in updates:
            setattr(user, field, updates[field])
    await db.flush()
    return user
