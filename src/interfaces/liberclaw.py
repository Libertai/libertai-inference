import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, model_validator


class LiberclawApiKeyRequest(BaseModel):
    user_id: str
    user_type: str
    # LiberClaw's own users.id. Identity bridge to Invoice.liberclaw_account_id —
    # stored on the LiberclawUser row when provided (never overwritten once set).
    liberclaw_account_id: uuid.UUID | None = None


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


class LiberclawInvoiceIssueRequest(BaseModel):
    liberclaw_account_id: uuid.UUID
    email: EmailStr
    tier: str
    period_start: datetime | None = None
    period_end: datetime | None = None
    # Direct path: the settled order to invoice. Sweep/backfill path: a subscription (+
    # optional cycle, to target a past one) that inference resolves to its order itself.
    order_id: str | None = None
    provider_subscription_id: str | None = None
    cycle_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_order_reference(self) -> "LiberclawInvoiceIssueRequest":
        if (self.order_id is None) == (self.provider_subscription_id is None):
            raise ValueError("Provide exactly one of order_id or provider_subscription_id")
        if self.cycle_id is not None and self.order_id is not None:
            raise ValueError("cycle_id may only accompany provider_subscription_id")
        return self


class IssueResult(BaseModel):
    status: str
    invoice_id: uuid.UUID | None = None
    number: str | None = None


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
