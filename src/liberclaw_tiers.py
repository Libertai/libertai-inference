# Tier names are LiberClaw's own plan names, exchanged verbatim over the /liberclaw endpoints.
LIBERCLAW_TIERS: dict[str, dict] = {
    "free": {"credits_limit": 10.0, "rolling_window_days": 30},
    "starter": {"credits_limit": 100.0, "rolling_window_days": 30},
    "pro": {"credits_limit": 500.0, "rolling_window_days": 30},
    "team": {"credits_limit": 2000.0, "rolling_window_days": 30},
}


def get_tier_config(tier: str) -> dict:
    """Config of a stored tier name, falling back to free for an unknown one."""
    return LIBERCLAW_TIERS.get(tier, LIBERCLAW_TIERS["free"])
