from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, PlainSerializer

# Reset/period timestamps come from naive ``TIMESTAMP`` columns (naive UTC on our UTC
# hosts). Serialize them as tz-aware UTC so clients get an unambiguous instant: a naive
# ISO string carries no offset, and JS ``new Date(s)`` parses an offset-less datetime as
# *browser-local* time — which skewed reset countdowns by the client's UTC offset and
# showed "Resets now" while the window was still live.
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(
        lambda dt: (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat(),
        return_type=str,
        when_used="json",
    ),
]


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


class RegionResponse(BaseModel):
    currency: str  # "USD" | "EUR"
    vat_rate: float  # display-only; Revolut applies the actual VAT


class TopupRequest(BaseModel):
    provider: str = "revolut"
    # USD regions: arbitrary amount, credited 1:1 (10k sanity cap). EUR regions: omit and send pack_id instead.
    amount: Annotated[float, Field(gt=0, le=10_000)] | None = None
    # Fixed EUR pack id (see GET /payments/topup-packs); required for EUR regions.
    pack_id: str | None = None
    # Origin of the app the user paid from (chat vs console); checkout returns there if allowlisted.
    redirect_base: str | None = None


class TopupPackResponse(BaseModel):
    id: str
    usd_credits: float  # credited to the (USD-denominated) prepaid balance
    eur_charge: float  # gross EUR charged (VAT-inclusive)


class SubscribeRequest(BaseModel):
    provider: str = "revolut"
    tier: str
    # Origin of the app the user paid from (chat vs console); checkout returns there if allowlisted.
    redirect_base: str | None = None


class DowngradeRequest(BaseModel):
    tier: str


class CheckoutResponse(BaseModel):
    checkout_url: str | None = None


class SubscriptionResponse(BaseModel):
    """Current subscription state. ``tier`` is the effective entitlement tier."""

    tier: str
    has_subscription: bool
    status: str | None = None
    provider: str | None = None
    current_period_end: UtcDatetime | None = None
    cancel_at_period_end: bool = False
    pending_tier: str | None = None
    is_trial: bool = False
    is_team_seat: bool = False
    team_name: str | None = None
    # Live gateway decision for the next call: lets the UI show the paywall directly.
    allowed: bool = True
    source: str = "tier"  # "tier" | "prepaid" | "blocked"
    # Dual-window allowance snapshot (free tier by default, larger if subscribed).
    window_5h_used: float = 0.0
    window_5h_limit: float = 0.0
    window_5h_resets_at: UtcDatetime | None = None
    weekly_used: float = 0.0
    weekly_limit: float = 0.0
    weekly_resets_at: UtcDatetime | None = None
    prepaid_balance: float = 0.0


class CancelResponse(BaseModel):
    message: str
    effective_date: UtcDatetime | None = None


class ResumeResponse(BaseModel):
    message: str
    tier: str


class DowngradeResponse(BaseModel):
    new_tier: str
    effective_date: UtcDatetime | None = None
