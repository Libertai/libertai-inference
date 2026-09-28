from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.liberclaw_user import LiberclawUser

# Tier names are LiberClaw's own plan names, exchanged verbatim over the /liberclaw endpoints.
LIBERCLAW_TIERS: dict[str, dict] = {
    "free": {"credits_limit": 5.0, "rolling_window_days": 30},
    "starter": {"credits_limit": 30.0, "rolling_window_days": 30},
    "pro": {"credits_limit": 100.0, "rolling_window_days": 30},
    "team": {"credits_limit": 300.0, "rolling_window_days": 30},
}


def get_tier_config(tier: str) -> dict:
    """Config of a stored tier name, falling back to free for an unknown one."""
    return LIBERCLAW_TIERS.get(tier, LIBERCLAW_TIERS["free"])


def effective_credits_limit(lc_user: "LiberclawUser") -> float:
    """Rolling-window cap a user actually has: their grandfathered allowance while one is set,
    else their tier's. The single read path for the cap — every enforcement and proration
    must go through here, or a grandfathered subscriber is silently cut to the new limit."""
    if lc_user.credits_limit_override is not None:
        return lc_user.credits_limit_override
    return get_tier_config(lc_user.tier)["credits_limit"]
