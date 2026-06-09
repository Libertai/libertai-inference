"""Tier table is Free/Go/Plus in USD, ordered, with Go/Plus paid."""

from src.subscription_tiers import (
    DEFAULT_CURRENCY,
    PAID_TIERS,
    SUBSCRIPTION_TIERS,
    TIER_ORDER,
    get_tier,
    is_upgrade,
)


def test_tiers_are_free_go_plus_usd():
    assert set(SUBSCRIPTION_TIERS) == {"free", "go", "plus"}
    assert DEFAULT_CURRENCY == "USD"
    assert all(t.currency == "USD" for t in SUBSCRIPTION_TIERS.values())


def test_tier_prices_and_windows():
    free, go, plus = get_tier("free"), get_tier("go"), get_tier("plus")
    assert (free.price_cents, free.window_5h_credits, free.weekly_credits) == (0, 0.5, 2.0)
    assert (go.price_cents, go.window_5h_credits, go.weekly_credits) == (800, 2.5, 5.0)
    assert (plus.price_cents, plus.window_5h_credits, plus.weekly_credits) == (2000, 5.0, 12.0)


def test_order_and_paid_set():
    assert TIER_ORDER == {"free": 0, "go": 1, "plus": 2}
    assert PAID_TIERS == {"go", "plus"}
    assert is_upgrade("free", "plus") is True
    assert is_upgrade("plus", "go") is False
