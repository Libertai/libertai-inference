import uuid

import pytest
from sqlalchemy import delete, select

from src.interfaces.device_tokens import DevicePlatform
from src.models.base import AsyncSessionLocal
from src.models.device_token import DeviceToken
from src.models.user import User
from src.services.auth_tokens import create_access_token

pytestmark = pytest.mark.asyncio


async def _create_user(email: str) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        user = User(email=email, email_verified=True)
        db.add(user)
        await db.commit()
        return user.id


async def _cleanup(token: str, *user_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeviceToken).where(DeviceToken.token == token))
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.commit()


async def test_register_reregister_and_delete_device_token(async_client):
    token = f"device-token-{uuid.uuid4().hex}"
    first_user = await _create_user(f"device-a-{uuid.uuid4().hex}@example.com")
    second_user = await _create_user(f"device-b-{uuid.uuid4().hex}@example.com")

    try:
        first_auth = {"authorization": f"Bearer {create_access_token(first_user)}"}
        second_auth = {"authorization": f"Bearer {create_access_token(second_user)}"}

        response = await async_client.post(
            "/devices",
            json={"token": token, "platform": "ios", "app_version": "1.0.0"},
            headers=first_auth,
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is True

        response = await async_client.post(
            "/devices",
            json={"token": token, "platform": "android", "app_version": "2.0.0"},
            headers=second_auth,
        )
        assert response.status_code == 200

        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(DeviceToken).where(DeviceToken.token == token))).scalars().one()
            assert row.user_id == second_user
            assert row.platform == DevicePlatform.android
            assert row.app_version == "2.0.0"
            assert row.enabled is True

        response = await async_client.delete(f"/devices/{token}", headers=first_auth)
        assert response.status_code == 204
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(DeviceToken).where(DeviceToken.token == token))).scalars().one()
            assert row.enabled is True

        response = await async_client.delete(f"/devices/{token}", headers=second_auth)
        assert response.status_code == 204
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(DeviceToken).where(DeviceToken.token == token))).scalars().one()
            assert row.enabled is False
    finally:
        await _cleanup(token, first_user, second_user)
