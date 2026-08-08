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


class LiberclawApiKeyDeactivateResponse(BaseModel):
    deactivated: bool


class LiberclawExtraCreditsGrant(BaseModel):
    user_id: str
    user_type: str
    # Tier whose window cap the grant is derived from (the plan being upgraded away).
    from_tier: str
    # Unused fraction of the old plan cycle, in (0, 1].
    unused_fraction: float
    # Idempotency key, e.g. "upgrade_remainder:<subscription-id>".
    external_reference: str


class LiberclawExtraCreditsResponse(BaseModel):
    amount: float


class LiberclawUserResponse(BaseModel):
    id: uuid.UUID
    user_id: str
    user_type: str
    tier: str
    credits_used: float
    credits_limit: float
    rolling_window_days: int
    # Unconsumed granted extra credits (usable once credits_used exceeds credits_limit).
    extra_credits_left: float = 0.0
    created_at: datetime
    # Last inference call on any of this user's keys, over all time rather than
    # the rolling window. Every path an agent can be driven by — chat, Telegram,
    # direct link — ends in a call here, so this is the only complete record of
    # whether the user has ever actually used anything.
    last_call_at: datetime | None = None
