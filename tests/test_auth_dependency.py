import pytest
from fastapi import HTTPException
from libertai_utils.interfaces.blockchain import LibertaiChain
from sqlalchemy import select

from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.models.wallet_connection import WalletConnection
from src.services.auth import create_access_token as create_legacy_token
from src.services.auth import get_current_user, get_optional_user
from src.services.auth_tokens import create_access_token


async def test_uuid_token_resolves_user():
    async with AsyncSessionLocal() as db:
        user = User(email="dep-test@example.com")
        db.add(user)
        await db.commit()
        user_id = user.id

    resolved = await get_current_user(authorization=f"Bearer {create_access_token(user_id)}")
    assert resolved.id == user_id


async def test_legacy_wallet_token_resolves_to_wallet_user():
    address = "0x1111111111111111111111111111111111111111"
    legacy = create_legacy_token(address, LibertaiChain.base)

    resolved = await get_current_user(authorization=f"Bearer {legacy}")

    async with AsyncSessionLocal() as db:
        wallet = (
            await db.execute(select(WalletConnection).where(WalletConnection.user_id == resolved.id))
        ).scalars().first()
        assert wallet is not None  # legacy address token created/linked a wallet user


async def test_missing_and_invalid_tokens_rejected():
    with pytest.raises(HTTPException):
        await get_current_user(authorization=None, libertai_auth=None)
    with pytest.raises(HTTPException):
        await get_current_user(authorization="Bearer not.a.jwt", libertai_auth=None)


async def test_optional_user_returns_none_without_valid_token():
    assert await get_optional_user(authorization=None, libertai_auth=None) is None
    assert await get_optional_user(authorization="Bearer not.a.jwt", libertai_auth=None) is None
