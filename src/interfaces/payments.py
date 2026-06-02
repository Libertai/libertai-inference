from datetime import datetime

from pydantic import BaseModel, Field


class PaymentProviderResponse(BaseModel):
    id: str
    kind: str  # "fiat" | "crypto"
    label: str
    capabilities: list[str]  # "topup" | "subscription"
    currencies: list[str]
    chain: str | None = None
    contract_address: str | None = None


class TierResponse(BaseModel):
    name: str
    price_cents: int
    currency: str
    window_5h_credits: float
    weekly_credits: float
    is_paid: bool


class TopupRequest(BaseModel):
    provider: str = "revolut"
    amount: float = Field(gt=0)


class SubscribeRequest(BaseModel):
    provider: str = "revolut"
    tier: str


class DowngradeRequest(BaseModel):
    tier: str


class CheckoutResponse(BaseModel):
    checkout_url: str


class SubscriptionResponse(BaseModel):
    """Current subscription state. ``tier`` is the effective entitlement tier."""

    tier: str
    has_subscription: bool
    status: str | None = None
    provider: str | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    pending_tier: str | None = None
    is_trial: bool = False


class CancelResponse(BaseModel):
    message: str
    effective_date: datetime | None = None


class DowngradeResponse(BaseModel):
    new_tier: str
    effective_date: datetime | None = None
