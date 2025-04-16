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


class InferenceCallData(BaseModel):
    key: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    model_name: str


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


class ApiKeyAdminListResponse(BaseModel):
    keys: list[str]
