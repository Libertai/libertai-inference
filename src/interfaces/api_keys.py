import uuid
from datetime import datetime

from pydantic import BaseModel


class ApiKeyCreate(BaseModel):
    name: str
    monthly_limit: float | None = None


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    monthly_limit: float | None = None


class ApiKeyUsageLog(BaseModel):
    key: str
    credits_used: float


class ApiKeyUsageResponse(BaseModel):
    id: int
    api_key_id: uuid.UUID
    credits_used: float
    used_at: datetime


class ApiKey(BaseModel):
    id: uuid.UUID
    key: str  # Masked key for display
    name: str
    user_address: str
    created_at: datetime
    is_active: bool
    monthly_limit: float | None = None


class FullApiKey(ApiKey):
    full_key: str


class ApiKeyListResponse(BaseModel):
    keys: list[ApiKey]
