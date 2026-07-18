import uuid
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel


class ApiKeyType(str, Enum):
    api = "api"
    chat = "chat"
    liberclaw = "liberclaw"
    x402 = "x402"
    cli = "cli"
    pool = "pool"


class InvalidKeyReason(str, Enum):
    """Why a real, non-deleted key is currently unusable (distributed with the whitelist)."""

    disabled = "disabled"
    expired = "expired"
    key_monthly_limit = "key_monthly_limit"
    no_credits = "no_credits"
    extra_credit_cap = "extra_credit_cap"
    liberclaw_limit = "liberclaw_limit"


# Static strings on purpose: the map is re-distributed every refresh cycle,
# dynamic content (timestamps) would churn the payload.
INVALID_KEY_MESSAGES: dict[InvalidKeyReason, str] = {
    InvalidKeyReason.disabled: "This API key has been disabled.",
    InvalidKeyReason.expired: "This API key has expired.",
    InvalidKeyReason.key_monthly_limit: "This API key reached its monthly usage limit.",
    InvalidKeyReason.no_credits: "Usage window limit reached and no extra credits available.",
    InvalidKeyReason.extra_credit_cap: "Monthly extra-credits cap reached.",
    InvalidKeyReason.liberclaw_limit: "Usage limit for your plan reached.",
}


class InferenceKeyType(str, Enum):
    """Key types whose usage is recorded in ``inference_calls`` (everything but chat).

    Used as a stats route path param so global inference stats are served by a single
    set of routes; invalid / chat values are rejected with HTTP 422 automatically.
    """

    api = "api"
    liberclaw = "liberclaw"
    x402 = "x402"
    cli = "cli"


class InferenceCallType(str, Enum):
    text = "text"
    image = "image"
    audio = "audio"


class InvalidKeyInfo(BaseModel):
    reason: InvalidKeyReason
    message: str


def invalid_key_info(reason: InvalidKeyReason) -> InvalidKeyInfo:
    return InvalidKeyInfo(reason=reason, message=INVALID_KEY_MESSAGES[reason])


class ApiKeyCreate(BaseModel):
    name: str
    monthly_limit: float | None = None


class CliApiKeyCreate(BaseModel):
    # Optional device label; the key is named "libertai-cli@<host>" and rotated in place.
    host: str | None = None


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
    payment_payload: str | None = None
    payment_requirements: str | None = None


class ImageInferenceCallData(BaseModel):
    key: str
    model_name: str
    image_count: int
    type: InferenceCallType = InferenceCallType.image
    payment_payload: str | None = None
    payment_requirements: str | None = None


class AudioInferenceCallData(BaseModel):
    """TTS usage as reported by libertai-models: input_tokens carries the character count
    of the synthesized text; there is no output side. The required Literal type keeps the
    union unambiguous, and the zero defaults let audio flow through the text billing
    branches (calculate_price routes to audio pricing by model)."""

    key: str
    model_name: str
    input_tokens: int  # character count
    output_tokens: int = 0
    cached_tokens: int = 0
    type: Literal[InferenceCallType.audio]
    payment_payload: str | None = None
    payment_requirements: str | None = None


# Union type for the API endpoint
InferenceCallData = TextInferenceCallData | ImageInferenceCallData | AudioInferenceCallData


class ApiKey(BaseModel):
    id: uuid.UUID
    key: str  # Masked key for display
    name: str
    user_id: uuid.UUID | None = None
    user_address: str | None = None
    created_at: datetime
    is_active: bool
    monthly_limit: float | None = None
    type: ApiKeyType
    expires_at: datetime | None = None


class FullApiKey(ApiKey):
    full_key: str


class ApiKeyListResponse(BaseModel):
    keys: list[FullApiKey]


class ApiKeyAdminListResponse(BaseModel):
    keys: list[str]
    invalid_keys: dict[str, InvalidKeyInfo] = {}


class ChatApiKeyResponse(BaseModel):
    key: str
