from sqlalchemy import func, select

from src.models.wallet_connection import WalletConnection
from src.services.oauth import OAuthUserInfo
from src.services.users import (
    get_or_create_user_by_email,
    get_or_create_user_by_oauth,
    link_wallet,
)


def _oauth_info(provider="google", provider_id="oauth-1", email="oauth1@example.com"):
    return OAuthUserInfo(
        provider=provider,
        provider_id=provider_id,
        email=email,
        email_verified=True,
        name="OAuth User",
        avatar_url="http://avatar",
    )


async def _wallet_count(db, user_id) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(WalletConnection).where(WalletConnection.user_id == user_id)
        )
    ).scalar()


async def test_oauth_user_created_without_wallet():
    from src.models.base import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        user, created = await get_or_create_user_by_oauth(db, _oauth_info(provider_id="new-1"))
        await db.commit()
        assert created is True
        assert user.email == "oauth1@example.com"
        assert await _wallet_count(db, user.id) == 0

        # second login: same user, not created
        user2, created2 = await get_or_create_user_by_oauth(db, _oauth_info(provider_id="new-1"))
        assert created2 is False
        assert user2.id == user.id


async def test_oauth_links_to_existing_email_user():
    from src.models.base import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        email_user, _ = await get_or_create_user_by_email(db, "shared@example.com")
        await db.commit()

        oauth_user, created = await get_or_create_user_by_oauth(
            db, _oauth_info(provider_id="link-1", email="shared@example.com")
        )
        await db.commit()
        assert created is False
        assert oauth_user.id == email_user.id


async def test_link_wallet_adds_one():
    from src.models.base import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, "wallet-link@example.com")
        await db.commit()
        assert await _wallet_count(db, user.id) == 0

        await link_wallet(db, user, "0x2222222222222222222222222222222222222222")
        await db.commit()
        assert await _wallet_count(db, user.id) == 1
