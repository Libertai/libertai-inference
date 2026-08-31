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
    # End of the paid subscription's current cycle, None on the free tier. A renewing
    # subscription moves it forward each cycle; it is the date the plan stops only for a
    # subscription already set to cancel.
    current_period_end: UtcDatetime | None
    # Prepaid balance still spendable once the plan allowance runs out, with the account's
    # monthly extra-credit cap already applied — so it is what the next call can actually draw on.
    extra_usage_credits: float
