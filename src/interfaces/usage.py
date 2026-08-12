from pydantic import BaseModel

from src.interfaces.common import UtcDatetime


class UsageWindow(BaseModel):
    """One allowance window's fill level. Shares only the share: the credit amounts
    behind it (spend and plan limit) stay server-side."""

    used_percent: float  # 0-100, clamped
    resets_at: UtcDatetime | None  # None while no window of this kind is open


class UsageResponse(BaseModel):
    """A key owner's own allowance state."""

    plan: str
    window_5h: UsageWindow
    weekly: UsageWindow
    # Prepaid balance still spendable once the plan allowance runs out, with the account's
    # monthly extra-credit cap already applied — so it is what the next call can actually draw on.
    extra_usage_credits: float
