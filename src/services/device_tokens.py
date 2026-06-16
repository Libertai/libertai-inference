import uuid
from datetime import datetime

from sqlalchemy import select

from src.interfaces.device_tokens import DeviceTokenRegisterRequest, DeviceTokenResponse
from src.models.base import AsyncSessionLocal
from src.models.device_token import DeviceToken
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def _to_response(device: DeviceToken) -> DeviceTokenResponse:
    return DeviceTokenResponse(
        token=device.token,
        platform=device.platform,
        app_version=device.app_version,
        enabled=device.enabled,
        created_at=device.created_at,
        updated_at=device.updated_at,
    )


class DeviceTokenService:
    @staticmethod
    async def register_device_token(user_id: uuid.UUID, payload: DeviceTokenRegisterRequest) -> DeviceTokenResponse:
        async with AsyncSessionLocal() as db:
            existing = (
                (await db.execute(select(DeviceToken).where(DeviceToken.token == payload.token))).scalars().first()
            )

            if existing is None:
                device = DeviceToken(
                    token=payload.token,
                    user_id=user_id,
                    platform=payload.platform,
                    app_version=payload.app_version,
                    enabled=True,
                )
                db.add(device)
            else:
                device = existing
                device.user_id = user_id
                device.platform = payload.platform
                device.app_version = payload.app_version
                device.enabled = True
                device.updated_at = datetime.now()

            await db.commit()
            await db.refresh(device)
            return _to_response(device)

    @staticmethod
    async def disable_device_token(user_id: uuid.UUID, token: str) -> None:
        async with AsyncSessionLocal() as db:
            existing = (
                (
                    await db.execute(
                        select(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user_id)
                    )
                )
                .scalars()
                .first()
            )
            if existing is None:
                return

            existing.enabled = False
            existing.updated_at = datetime.now()
            await db.commit()
