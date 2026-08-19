"""Updating an API key only touches the fields the request actually carried."""

import uuid

import pydantic
import pytest
from sqlalchemy import delete

from src.interfaces.api_keys import ApiKeyType, ApiKeyUpdate
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.api_key import ApiKeyService


async def _user_with_key(monthly_limit: float | None) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as db:
        user = User(email=f"pu-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.flush()
        key = ApiKeyDB(
            key=ApiKeyDB.generate_key(),
            name="original",
            user_id=user.id,
            type=ApiKeyType.api,
            monthly_limit=monthly_limit,
        )
        db.add(key)
        await db.flush()
        await db.commit()
        return user.id, key.id


async def _cleanup(user_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


def test_update_request_partial_dump():
    dump = ApiKeyUpdate.model_validate({"name": "renamed"}).model_dump(exclude_unset=True)
    assert dump == {"name": "renamed"}
    dump = ApiKeyUpdate.model_validate({"monthly_limit": None}).model_dump(exclude_unset=True)
    assert dump == {"monthly_limit": None}


def test_update_request_rejects_explicit_null_on_not_null_columns():
    with pytest.raises(pydantic.ValidationError):
        ApiKeyUpdate.model_validate({"name": None})
    with pytest.raises(pydantic.ValidationError):
        ApiKeyUpdate.model_validate({"is_active": None})


async def test_monthly_limit_cleared_and_zero_kept():
    user_id, key_id = await _user_with_key(50.0)
    try:
        updated = await ApiKeyService.update_api_key(key_id, {"monthly_limit": None})
        assert updated is not None
        assert updated.monthly_limit is None

        updated = await ApiKeyService.update_api_key(key_id, {"monthly_limit": 0})
        assert updated is not None
        assert updated.monthly_limit == 0
    finally:
        await _cleanup(user_id)


async def test_absent_fields_left_untouched():
    user_id, key_id = await _user_with_key(20.0)
    try:
        updated = await ApiKeyService.update_api_key(key_id, {"name": "renamed"})
        assert updated is not None
        assert updated.name == "renamed"
        assert updated.monthly_limit == 20.0
        assert updated.is_active is True
    finally:
        await _cleanup(user_id)
