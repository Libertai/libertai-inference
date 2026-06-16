from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class DevicePlatform(str, Enum):
    ios = "ios"
    android = "android"


class DeviceTokenRegisterRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    platform: DevicePlatform
    app_version: str | None = Field(default=None, max_length=100)

    @field_validator("token")
    def normalize_token(cls, value: str) -> str:
        token = value.strip()
        if not token:
            raise ValueError("token must not be empty")
        return token

    @field_validator("app_version")
    def normalize_app_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        version = value.strip()
        return version or None


class DeviceTokenResponse(BaseModel):
    token: str
    platform: DevicePlatform
    app_version: str | None = None
    enabled: bool
    created_at: datetime
    updated_at: datetime
