import uuid
from datetime import datetime

from pydantic import BaseModel


class LiberclawApiKeyRequest(BaseModel):
    user_id: str
    user_type: str


class LiberclawTierUpdate(BaseModel):
    user_id: str
    user_type: str
    tier: str


class LiberclawApiKeyResponse(BaseModel):
    key: str
    is_new: bool


class LiberclawUserResponse(BaseModel):
    id: uuid.UUID
    user_id: str
    user_type: str
    tier: str
    credits_used: float
    credits_limit: float
    rolling_window_days: int
    created_at: datetime
