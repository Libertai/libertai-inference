import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class ApiKeyType(str, Enum):
    api = "api"
    chat = "chat"
    liberclaw = "liberclaw"
    x402 = "x402"


class InferenceCallType(str, Enum):
    text = "text"
    image = "image"


class ApiKeyCreate(BaseModel):
    name: str
    monthly_limit: float | None = None


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    monthly_limit: float | None = None


class TextInferenceCallData(BaseModel):
    key: str
    model_name: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    type: InferenceCallType | None = None  # Optional for backward compatibility
    payment_amount: float | None = None
    payer_address: str | None = None


class ImageInferenceCallData(BaseModel):
    key: str
    model_name: str
    image_count: int
    type: InferenceCallType = InferenceCallType.image
    payment_amount: float | None = None
    payer_address: str | None = None


# Union type for the API endpoint
InferenceCallData = TextInferenceCallData | ImageInferenceCallData


class ApiKey(BaseModel):
    id: uuid.UUID
    key: str  # Masked key for display
    name: str
    user_address: str | None = None
    created_at: datetime
    is_active: bool
    monthly_limit: float | None = None
    type: ApiKeyType


class FullApiKey(ApiKey):
    full_key: str


class ApiKeyListResponse(BaseModel):
    keys: list[FullApiKey]


class ApiKeyAdminListResponse(BaseModel):
    keys: list[str]


class ChatApiKeyResponse(BaseModel):
    key: str
