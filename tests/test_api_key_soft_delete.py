"""Soft-deleting an API key hides it but preserves its usage history."""

import uuid

import pytest
from sqlalchemy import delete, func, select

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.api_key import ApiKeyService

pytestmark = pytest.mark.asyncio


async def _user_with_key() -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as db:
        user = User(email=f"sd-{uuid.uuid4().hex}@example.com", email_verified=True)
        db.add(user)
        await db.flush()
        key = ApiKeyDB(key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, user_id=user.id, type=ApiKeyType.api)
        db.add(key)
        await db.flush()
        db.add(InferenceCall(api_key_id=key.id, credits_used=0.3, model_name="m"))
        await db.commit()
        return user.id, key.id


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_soft_delete_hides_key_but_keeps_usage():
    user_id, key_id = await _user_with_key()
    try:
        assert await ApiKeyService.delete_api_key(key_id) is True

        # Hidden from the user's list.
        listed = await ApiKeyService.get_api_keys(user_id)
        assert all(k.id != key_id for k in listed)

        async with AsyncSessionLocal() as db:
            row = await db.get(ApiKeyDB, key_id)
            assert row is not None  # row preserved
            assert row.deleted_at is not None
            assert row.is_active is False

            usage_count = (
                await db.execute(
                    select(func.count()).select_from(InferenceCall).where(InferenceCall.api_key_id == key_id)
                )
            ).scalar()
            assert usage_count == 1  # usage history preserved

        # Excluded from the inference gateway.
        async with AsyncSessionLocal() as db:
            key_str = (await db.get(ApiKeyDB, key_id)).key
        assert key_str not in await ApiKeyService.get_admin_all_api_keys()
    finally:
        await _cleanup(user_id)


async def test_deleted_key_name_is_reusable():
    user_id, key_id = await _user_with_key()
    try:
        async with AsyncSessionLocal() as db:
            name = (await db.get(ApiKeyDB, key_id)).name
        await ApiKeyService.delete_api_key(key_id)
        # Same name can be created again after the original was soft-deleted.
        created = await ApiKeyService.create_api_key(user_id=user_id, name=name)
        assert created.name == name
    finally:
        await _cleanup(user_id)
