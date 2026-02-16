LIBERCLAW_TIERS: dict[str, dict] = {
    "free": {"credits_limit": 1.0, "rolling_window_days": 30},
    "premium": {"credits_limit": 10.0, "rolling_window_days": 30},
    "pro": {"credits_limit": 50.0, "rolling_window_days": 30},
    "ultra": {"credits_limit": 200.0, "rolling_window_days": 30},
}
