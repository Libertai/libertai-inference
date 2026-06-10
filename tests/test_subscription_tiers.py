"""Tier table is Free/Go/Plus/Power in USD, ordered, with Go/Plus/Power paid."""

import pytest

from src.subscription_tiers import (
    DEFAULT_CURRENCY,
    PAID_TIERS,
    SUBSCRIPTION_TIERS,
    TIER_ORDER,
    get_provider_plan,
    get_tier,
    is_upgrade,
)


def test_tiers_are_free_go_plus_power_usd():
    assert set(SUBSCRIPTION_TIERS) == {"free", "go", "plus", "power"}
    assert DEFAULT_CURRENCY == "USD"
    assert all(t.currency == "USD" for t in SUBSCRIPTION_TIERS.values())


def test_tier_prices_and_windows():
    free, go, plus, power = get_tier("free"), get_tier("go"), get_tier("plus"), get_tier("power")
    assert (free.price_cents, free.window_5h_credits, free.weekly_credits) == (0, 0.5, 2.0)
    assert (go.price_cents, go.window_5h_credits, go.weekly_credits) == (800, 2.5, 5.0)
    assert (plus.price_cents, plus.window_5h_credits, plus.weekly_credits) == (2000, 5.0, 12.0)
    assert (power.price_cents, power.window_5h_credits, power.weekly_credits) == (10000, 25.0, 60.0)


def test_get_provider_plan_per_currency():
    usd = get_provider_plan("go", "revolut", "USD")
    assert set(usd) == {"plan_id", "variation_id"}
    assert usd["plan_id"] and usd["variation_id"]

    eur = get_provider_plan("go", "revolut", "EUR")
    assert set(eur) == {"plan_id", "variation_id"}
    assert eur != usd


def test_get_provider_plan_unknown_currency_raises():
    with pytest.raises(ValueError, match="GBP"):
        get_provider_plan("go", "revolut", "GBP")


def test_get_provider_plan_unknown_provider_raises():
    with pytest.raises(ValueError, match="stripe"):
        get_provider_plan("go", "stripe", "USD")


def test_get_provider_plan_free_tier_raises():
    with pytest.raises(ValueError):
        get_provider_plan("free", "revolut", "USD")


def test_order_and_paid_set():
    assert TIER_ORDER == {"free": 0, "go": 1, "plus": 2, "power": 3}
    assert PAID_TIERS == {"go", "plus", "power"}
    assert is_upgrade("free", "power") is True
    assert is_upgrade("power", "go") is False
