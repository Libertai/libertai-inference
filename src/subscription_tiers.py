"""Subscription tier configuration (provider-agnostic).

A tier defines two things:
  1. Recurring **entitlement windows** (Phase 4) — a trailing-5h and trailing-7d
     credit allowance that every user gets (even with no paid subscription, via
     the ``free`` tier). Exhausting a window falls through to prepaid balance.
  2. **Provider plan IDs** — the per-provider identifiers needed to open a
     subscription checkout. Keyed by provider id so the same tier can be sold
     through Revolut today and another fiat provider tomorrow without touching
     the manager.

Pricing: Go $8 / Plus $20 / Max $100 per month (EUR plans are net the same
number; Revolut applies 20% VAT on the EUR variations).
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_TIER = "free"
DEFAULT_CURRENCY = "USD"


@dataclass(frozen=True)
class TierConfig:
    name: str
    price_cents: int
    currency: str
    # Rolling-window allowances in credit units (USD-equivalent), consumed by the
    # Phase 4 entitlement service. ``free`` gets small windows; paid tiers larger.
    window_5h_credits: float
    weekly_credits: float
    # provider id -> currency -> identifiers required to open a checkout on that
    # provider in that currency (Revolut plans have a fixed currency, so each
    # tier needs one plan per supported currency).
    # e.g. {"revolut": {"USD": {"plan_id": "...", "variation_id": "..."}}}
    provider_plan_ids: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        return self.price_cents > 0


SUBSCRIPTION_TIERS: dict[str, TierConfig] = {
    "free": TierConfig(
        name="free",
        price_cents=0,
        currency=DEFAULT_CURRENCY,
        window_5h_credits=0.5,
        weekly_credits=2.0,
        provider_plan_ids={},
    ),
    "go": TierConfig(
        name="go",
        price_cents=800,
        currency=DEFAULT_CURRENCY,
        window_5h_credits=2.5,
        weekly_credits=5.0,
        provider_plan_ids={
            # One Revolut plan per tier; currency is a plan VARIATION (EUR variation has 20% VAT).
            "revolut": {
                "USD": {
                    "plan_id": "dbdd77ea-f4c8-4b8d-9dca-c62341a68eae",
                    "variation_id": "18a5745f-a164-4377-be89-41151e0f1adc",
                },
                "EUR": {
                    "plan_id": "dbdd77ea-f4c8-4b8d-9dca-c62341a68eae",
                    "variation_id": "6665637b-43e5-4c30-9af3-0274aa5f49f5",
                },
            }
        },
    ),
    "plus": TierConfig(
        name="plus",
        price_cents=2000,
        currency=DEFAULT_CURRENCY,
        window_5h_credits=5.0,
        weekly_credits=12.0,
        provider_plan_ids={
            "revolut": {
                "USD": {
                    "plan_id": "e5d0da8d-1c48-4221-a72c-cf3a6a31aeab",
                    "variation_id": "af85b71a-1d40-43aa-9fe0-4c3844df7ad3",
                },
                "EUR": {
                    "plan_id": "e5d0da8d-1c48-4221-a72c-cf3a6a31aeab",
                    "variation_id": "bd805f0c-7562-43f2-8782-dbf2d239f5cd",
                },
            }
        },
    ),
    "max": TierConfig(
        name="max",
        price_cents=10000,
        currency=DEFAULT_CURRENCY,
        window_5h_credits=25.0,
        weekly_credits=60.0,
        provider_plan_ids={
            "revolut": {
                "USD": {
                    "plan_id": "7bfe3520-dd3c-4a02-aeb1-e35e525db28d",
                    "variation_id": "4b317f35-7c0c-41c3-90bb-2b12e9646207",
                },
                "EUR": {
                    "plan_id": "7bfe3520-dd3c-4a02-aeb1-e35e525db28d",
                    "variation_id": "0a21d429-a6af-4f0c-92eb-278c7ca0c8ce",
                },
            }
        },
    ),
}

# Higher index = higher tier (used for up/downgrade validation).
TIER_ORDER: dict[str, int] = {name: i for i, name in enumerate(SUBSCRIPTION_TIERS)}
PAID_TIERS: set[str] = {name for name, cfg in SUBSCRIPTION_TIERS.items() if cfg.is_paid}


def get_tier(tier: str) -> TierConfig:
    cfg = SUBSCRIPTION_TIERS.get(tier)
    if cfg is None:
        raise ValueError(f"Unknown tier: {tier}")
    return cfg


def get_provider_plan(tier: str, provider: str, currency: str) -> dict[str, str]:
    """Return the {plan_id, variation_id} for a tier on a given provider in a given currency."""
    plan = get_tier(tier).provider_plan_ids.get(provider, {}).get(currency)
    if not plan:
        raise ValueError(f"Tier {tier!r} is not sold through provider {provider!r} in currency {currency!r}")
    if any(value.startswith("TODO") for value in plan.values()):
        raise ValueError(
            f"Plan ids for tier {tier!r} on provider {provider!r} in currency {currency!r} are not configured"
        )
    return plan


def is_upgrade(current_tier: str, new_tier: str) -> bool:
    return TIER_ORDER.get(new_tier, 0) > TIER_ORDER.get(current_tier, 0)


def is_downgrade(current_tier: str, new_tier: str) -> bool:
    return TIER_ORDER.get(new_tier, 0) < TIER_ORDER.get(current_tier, 0)
