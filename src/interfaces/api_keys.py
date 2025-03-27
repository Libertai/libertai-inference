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
    key_id: str
    credits_used: float


class ApiKeyUsageResponse(BaseModel):
    id: int
    key_id: str
    credits_used: float
    used_at: datetime


class ApiKeyResponse(BaseModel):
    key_id: str
    name: str
    address: str
    created_at: datetime
    is_active: bool
    monthly_limit: float | None = None


class ApiKeyListResponse(BaseModel):
    keys: list[ApiKeyResponse]
