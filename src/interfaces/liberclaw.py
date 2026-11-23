import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class LiberclawApiKeyRequest(BaseModel):
    user_id: str
    user_type: str
    # LiberClaw's own users.id. Identity bridge to Invoice.liberclaw_account_id —
    # stored on the LiberclawUser row when provided (never overwritten once set).
    liberclaw_account_id: uuid.UUID | None = None


class LiberclawApiKeyResponse(BaseModel):
    key: str
    is_new: bool


class LiberclawApiKeyDeactivateResponse(BaseModel):
    deactivated: bool


class LiberclawSuspendedAccount(BaseModel):
    # LiberClaw's users.id, and the identity the api-key call uses. Both are sent because
    # many rows carry no liberclaw_account_id; a row matches on either.
    account_id: uuid.UUID
    user_id: str
    user_type: str


class LiberclawSuspendedAccountsRequest(BaseModel):
    # The complete set of suspended LiberClaw accounts: every account not listed is live.
    accounts: list[LiberclawSuspendedAccount]


class LiberclawSuspendedAccountsResponse(BaseModel):
    suspended: int
    unsuspended: int


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


class SubscriptionCycle(BaseModel):
    cycle_id: str
    order_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class SubscriptionCyclesResponse(BaseModel):
    cycles: list[SubscriptionCycle]


class LiberclawAccountRequest(BaseModel):
    liberclaw_account_id: uuid.UUID


class LiberclawTierRequest(LiberclawAccountRequest):
    tier: str


class LiberclawUpgradeRequest(LiberclawAccountRequest):
    tier: str
    redirect_url: str


class LiberclawCheckoutRequest(LiberclawAccountRequest):
    email: EmailStr
    tier: str
    redirect_url: str


class LiberclawCheckoutResponse(BaseModel):
    url: str | None
    subscription_id: str | None


class LiberclawTrialRequest(LiberclawAccountRequest):
    email: EmailStr
    # Mirrors grant_trial's own bound (1-90 days) — the manager never checks this one itself.
    days: int = Field(ge=1, le=90)


class LiberclawTrialEligibilityResponse(BaseModel):
    eligible: bool
    reason: str | None


class LiberclawAdminGrantTrialRequest(LiberclawAccountRequest):
    tier: str
    days: int
    granted_by: str | None = None


class LiberclawAdminExtendRequest(LiberclawAccountRequest):
    days: int


class LiberclawExtendResponse(BaseModel):
    new_period_end: str


class LiberclawAgentVerificationCheckoutRequest(LiberclawAccountRequest):
    email: EmailStr
    redirect_url: str


class LiberclawAgentVerificationCheckoutResponse(BaseModel):
    # None when a paid verification already exists (already_verified=True).
    url: str | None
    already_verified: bool = False


class LiberclawAgentVerificationStatusResponse(BaseModel):
    """Latest verification state for a LiberClaw account.

    ``status`` is one of ``pending`` | ``paid`` | ``failed`` | ``conflict``. ``paid``
    means verified. ``conflict`` means the card already verified another account — the
    $1 is kept as the anti-sybil cost, so it deserves its own user-facing message.
    """

    verified: bool
    status: str | None = None
    order_id: str | None = None
    paid_at: datetime | None = None
