import uuid

from pydantic import BaseModel, EmailStr, Field

from src.interfaces.payments import UtcDatetime


class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    seat_prices: dict[str, float] = {}
    extra_credits_monthly_cap: float | None = Field(default=None, ge=0)
    extra_credits_member_default_cap: float | None = Field(default=None, ge=0)


class TeamUpdateRequest(BaseModel):
    name: str | None = None
    seat_prices: dict[str, float] | None = None
    extra_credits_monthly_cap: float | None = Field(default=None, ge=0)
    extra_credits_member_default_cap: float | None = Field(default=None, ge=0)


class TeamResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    seat_prices: dict[str, float]
    extra_credits_monthly_cap: float | None
    extra_credits_member_default_cap: float | None


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = "member"


class InviteResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    expires_at: UtcDatetime


class AcceptInviteRequest(BaseModel):
    token: str


class RoleRequest(BaseModel):
    role: str


class SeatAssignRequest(BaseModel):
    user_id: uuid.UUID
    tier: str


class SeatChangeRequest(BaseModel):
    tier: str


class CapsRequest(BaseModel):
    extra_credits_monthly_cap: float | None = Field(default=None, ge=0)
    extra_credits_member_default_cap: float | None = Field(default=None, ge=0)


class MemberCapRequest(BaseModel):
    extra_credits_cap_override: float | None = Field(default=None, ge=0)


class TeamTopupRequest(BaseModel):
    amount: float = Field(gt=0)
    redirect_base: str | None = None


class MemberResponse(BaseModel):
    user_id: uuid.UUID
    email: str | None
    display_name: str | None
    role: str
    seat_tier: str | None
    seat_status: str | None
    seat_period_end: UtcDatetime | None
    extra_credits_cap_override: float | None


class TeamMeResponse(BaseModel):
    team: TeamResponse
    role: str
    balance: float | None = None  # admins only
    members: list[MemberResponse] | None = None  # admins only
    own_seat_tier: str | None = None
    own_seat_period_end: UtcDatetime | None = None


class LedgerChargeResponse(BaseModel):
    entry_type: str
    amount: float
    metadata: dict | None
    created_at: UtcDatetime


class LedgerTopupResponse(BaseModel):
    amount: float
    status: str
    created_at: UtcDatetime


class LedgerResponse(BaseModel):
    balance: float
    topups: list[LedgerTopupResponse]
    charges: list[LedgerChargeResponse]


class MemberUsageResponse(BaseModel):
    user_id: uuid.UUID
    email: str | None
    seat_tier: str | None
    window_5h_used: float
    weekly_used: float
    extra_credits_month_to_date: float
