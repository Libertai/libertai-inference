"""Tier table is Free/Go/Plus/Max in USD, ordered, with Go/Plus/Max paid."""

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


def test_tiers_are_free_go_plus_max_usd():
    assert set(SUBSCRIPTION_TIERS) == {"free", "go", "plus", "max"}
    assert DEFAULT_CURRENCY == "USD"
    assert all(t.currency == "USD" for t in SUBSCRIPTION_TIERS.values())


def test_tier_prices_and_windows():
    free, go, plus, max_ = get_tier("free"), get_tier("go"), get_tier("plus"), get_tier("max")
    assert (free.price_cents, free.window_5h_credits, free.weekly_credits) == (0, 0.5, 2.0)
    assert (go.price_cents, go.window_5h_credits, go.weekly_credits) == (800, 2.5, 10.0)
    assert (plus.price_cents, plus.window_5h_credits, plus.weekly_credits) == (2000, 7.0, 30.0)
    assert (max_.price_cents, max_.window_5h_credits, max_.weekly_credits) == (10000, 50.0, 300.0)


def test_get_provider_plan_per_currency():
    """Each paid tier sells through ONE Revolut plan with a per-currency variation."""
    for tier in ("go", "plus", "max"):
        usd = get_provider_plan(tier, "revolut", "USD")
        eur = get_provider_plan(tier, "revolut", "EUR")
        assert set(usd) == set(eur) == {"plan_id", "variation_id"}
        # Same plan, different (currency) variation.
        assert usd["plan_id"] == eur["plan_id"]
        assert usd["variation_id"] != eur["variation_id"]


def test_get_provider_plan_placeholder_ids_raise(monkeypatch):
    """The TODO guard stays: a tier whose ids are placeholders must never reach Revolut."""
    import src.subscription_tiers as tiers

    fake = dict(SUBSCRIPTION_TIERS)
    go = fake["go"]
    fake["go"] = type(go)(
        name=go.name,
        price_cents=go.price_cents,
        currency=go.currency,
        window_5h_credits=go.window_5h_credits,
        weekly_credits=go.weekly_credits,
        provider_plan_ids={"revolut": {"EUR": {"plan_id": "TODO_X", "variation_id": "TODO_Y"}}},
    )
    monkeypatch.setattr(tiers, "SUBSCRIPTION_TIERS", fake)
    with pytest.raises(ValueError, match="not configured"):
        get_provider_plan("go", "revolut", "EUR")


def _set_plan_override(monkeypatch, raw: str):
    import src.subscription_tiers as tiers
    from src.config import config

    monkeypatch.setattr(config, "REVOLUT_PLAN_IDS", raw)
    tiers._revolut_plan_overrides.cache_clear()


def test_env_override_replaces_revolut_plan_ids(monkeypatch):
    """REVOLUT_PLAN_IDS (e.g. sandbox ids on beta) wins over the in-code production ids."""
    import src.subscription_tiers as tiers

    _set_plan_override(
        monkeypatch,
        '{"go": {"USD": {"plan_id": "sbx_plan", "variation_id": "sbx_var"}}}',
    )
    try:
        assert get_provider_plan("go", "revolut", "USD") == {"plan_id": "sbx_plan", "variation_id": "sbx_var"}
        # Tiers/currencies absent from the override fall back to the in-code ids.
        assert get_provider_plan("plus", "revolut", "EUR")["plan_id"]
        assert get_provider_plan("go", "revolut", "EUR")["plan_id"] != "sbx_plan"
    finally:
        tiers._revolut_plan_overrides.cache_clear()


def test_env_override_bad_json_falls_back(monkeypatch):
    import src.subscription_tiers as tiers

    _set_plan_override(monkeypatch, "{not json")
    try:
        plan = get_provider_plan("go", "revolut", "USD")
        assert plan["plan_id"]  # in-code ids still served
    finally:
        tiers._revolut_plan_overrides.cache_clear()


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
    assert TIER_ORDER == {"free": 0, "go": 1, "plus": 2, "max": 3}
    assert PAID_TIERS == {"go", "plus", "max"}
    assert is_upgrade("free", "max") is True
    assert is_upgrade("max", "go") is False
