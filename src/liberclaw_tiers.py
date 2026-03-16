LIBERCLAW_TIERS: dict[str, dict] = {
    "free": {"credits_limit": 20.0, "rolling_window_days": 30},
    "premium": {"credits_limit": 100.0, "rolling_window_days": 30},
    "pro": {"credits_limit": 500.0, "rolling_window_days": 30},
    "ultra": {"credits_limit": 2000.0, "rolling_window_days": 30},
}
