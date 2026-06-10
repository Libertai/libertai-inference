"""Subscription tier configuration (provider-agnostic).

A tier defines two things:
  1. Recurring **entitlement windows** (Phase 4) — a trailing-5h and trailing-7d
     credit allowance that every user gets (even with no paid subscription, via
     the ``free`` tier). Exhausting a window falls through to prepaid balance.
  2. **Provider plan IDs** — the per-provider identifiers needed to open a
     subscription checkout. Keyed by provider id so the same tier can be sold
     through Revolut today and another fiat provider tomorrow without touching
     the manager.

All prices/windows/plan IDs here are placeholders until the Revolut product +
final pricing are confirmed (see the plan's Unresolved Questions).
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
            "revolut": {
                # NOTE: placeholder Revolut plan/variation UUIDs — replace before enabling Revolut.
                "USD": {
                    "plan_id": "a9a0b97f-753f-4e13-ac60-f86733809dce",
                    "variation_id": "88e34b68-abea-497a-9743-01874274dcdf",
                },
                # TODO: replace with real EUR plan ids (created in Revolut dashboard, net price + 20% VAT)
                "EUR": {"plan_id": "TODO_EUR_PLAN_GO", "variation_id": "TODO_EUR_VAR_GO"},
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
                # NOTE: placeholder Revolut plan/variation UUIDs — replace before enabling Revolut.
                "USD": {
                    "plan_id": "c4c23aef-c39d-419d-99b6-f84034102615",
                    "variation_id": "2bdb31f1-78d5-48ad-88eb-c9c41fac57ef",
                },
                # TODO: replace with real EUR plan ids (created in Revolut dashboard, net price + 20% VAT)
                "EUR": {"plan_id": "TODO_EUR_PLAN_PLUS", "variation_id": "TODO_EUR_VAR_PLUS"},
            }
        },
    ),
    "power": TierConfig(
        name="power",
        price_cents=10000,
        currency=DEFAULT_CURRENCY,
        window_5h_credits=25.0,
        weekly_credits=60.0,
        provider_plan_ids={
            "revolut": {
                # NOTE: placeholder Revolut plan/variation UUIDs — replace before enabling Revolut.
                "USD": {
                    "plan_id": "d66f42c8-5b08-4dc0-9bd1-8f17f3f70b7b",
                    "variation_id": "71a36c44-4277-495d-9258-6eba1c325559",
                },
                # TODO: replace with real EUR plan ids (created in Revolut dashboard, net price + 20% VAT)
                "EUR": {"plan_id": "TODO_EUR_PLAN_POWER", "variation_id": "TODO_EUR_VAR_POWER"},
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
    return plan


def is_upgrade(current_tier: str, new_tier: str) -> bool:
    return TIER_ORDER.get(new_tier, 0) > TIER_ORDER.get(current_tier, 0)


def is_downgrade(current_tier: str, new_tier: str) -> bool:
    return TIER_ORDER.get(new_tier, 0) < TIER_ORDER.get(current_tier, 0)
