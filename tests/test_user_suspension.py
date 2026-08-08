"""Suspended users lose gateway access and their sessions stop resolving."""

import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.liberclaw_user import LiberclawUser
from src.models.user import User
from src.services.api_key import ApiKeyService

pytestmark = pytest.mark.asyncio


async def _valid_keys() -> list[str]:
    return (await ApiKeyService.get_admin_all_api_keys()).valid


async def _user_with_key(*, suspended: bool = False, key_active: bool = True):
    async with AsyncSessionLocal() as db:
        user = User(email=f"susp-{uuid.uuid4().hex}@example.com")
        if suspended:
            user.suspended_at = datetime.now()
            user.suspension_reason = "test"
        db.add(user)
        await db.flush()
        key = ApiKeyDB(
            key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, user_id=user.id, type=ApiKeyType.api
        )
        key.is_active = key_active
        db.add(key)
        await db.commit()
        return user.id, key.key


async def _liberclaw_key() -> tuple[uuid.UUID, str]:
    async with AsyncSessionLocal() as db:
        lc = LiberclawUser(user_id=uuid.uuid4().hex, user_type="discord", tier="free")
        db.add(lc)
        await db.flush()
        key = ApiKeyDB(
            key=ApiKeyDB.generate_key(),
            name=uuid.uuid4().hex,
            type=ApiKeyType.liberclaw,
            liberclaw_user_id=lc.id,
        )
        db.add(key)
        await db.commit()
        return lc.id, key.key


async def _set_suspended(user_id, value: datetime | None) -> None:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        user.suspended_at = value
        await db.commit()


async def _cleanup(user_id) -> None:
    from sqlalchemy import delete

    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _cleanup_liberclaw(lc_id) -> None:
    from sqlalchemy import delete

    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawUser).where(LiberclawUser.id == lc_id))
        await db.commit()


async def test_suspended_users_key_is_not_whitelisted():
    user_id, key = await _user_with_key(suspended=True)
    try:
        assert key not in await _valid_keys()
    finally:
        await _cleanup(user_id)


async def test_suspended_users_key_gets_no_invalid_reason():
    """Dropped entirely rather than mapped to a reason, so the gateway 401s it as unknown."""
    user_id, key = await _user_with_key(suspended=True)
    try:
        assert key not in (await ApiKeyService.get_admin_all_api_keys()).invalid
    finally:
        await _cleanup(user_id)


async def test_liberclaw_key_survives_an_unrelated_suspension():
    """Liberclaw keys carry user_id=NULL; an inner join would drop them all."""
    suspended_id, _ = await _user_with_key(suspended=True)
    lc_id, lc_key = await _liberclaw_key()
    try:
        assert lc_key in await _valid_keys()
    finally:
        await _cleanup_liberclaw(lc_id)
        await _cleanup(suspended_id)


async def test_pool_key_survives_an_unrelated_suspension():
    from src.services.api_key_pool import POOL_SENTINEL_NAME

    suspended_id, _ = await _user_with_key(suspended=True)
    async with AsyncSessionLocal() as db:
        pool_key = ApiKeyDB.generate_key()
        db.add(ApiKeyDB(key=pool_key, name=POOL_SENTINEL_NAME, type=ApiKeyType.pool))
        await db.commit()
    try:
        assert pool_key in await _valid_keys()
    finally:
        from sqlalchemy import delete

        async with AsyncSessionLocal() as db:
            await db.execute(delete(ApiKeyDB).where(ApiKeyDB.key == pool_key))
            await db.commit()
        await _cleanup(suspended_id)


async def test_unsuspending_restores_the_key():
    user_id, key = await _user_with_key(suspended=True)
    try:
        await _set_suspended(user_id, None)
        assert key in await _valid_keys()
    finally:
        await _cleanup(user_id)


async def test_unsuspending_leaves_a_user_disabled_key_disabled():
    """Suspension is derived, never written to the key, so prior per-key state survives."""
    user_id, key = await _user_with_key(suspended=True, key_active=False)
    try:
        await _set_suspended(user_id, None)
        assert key not in await _valid_keys()
    finally:
        await _cleanup(user_id)


async def test_dot_variant_signup_returns_the_suspended_account():
    """A gmail dot-variant canonicalises to the same row, so the mailbox cannot be re-registered."""
    from src.services.users import get_or_create_user_by_email

    async with AsyncSessionLocal() as db:
        user = User(email="hunterwagn.e.r6.37.6@gmail.com")
        user.suspended_at = datetime.now()
        db.add(user)
        await db.commit()
        user_id = user.id
    try:
        async with AsyncSessionLocal() as db:
            found, created = await get_or_create_user_by_email(db, "hunterwa.gner6.37.6@gmail.com")
            assert not created
            assert found.id == user_id
            assert found.suspended_at is not None
    finally:
        await _cleanup(user_id)


async def test_suspended_user_token_is_rejected():
    from src.services.auth import _resolve_user_from_token
    from src.services.auth_tokens import create_access_token

    user_id, _ = await _user_with_key(suspended=True)
    try:
        token = create_access_token(user_id)
        with pytest.raises(HTTPException) as exc:
            await _resolve_user_from_token(token)
        assert exc.value.status_code == 401
    finally:
        await _cleanup(user_id)


async def test_active_user_token_still_resolves():
    from src.services.auth import _resolve_user_from_token
    from src.services.auth_tokens import create_access_token

    user_id, _ = await _user_with_key()
    try:
        token = create_access_token(user_id)
        assert (await _resolve_user_from_token(token)).id == user_id
    finally:
        await _cleanup(user_id)
