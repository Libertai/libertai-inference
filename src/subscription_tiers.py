"""Subscription tier configuration (provider-agnostic).

A tier defines two things:
  1. Recurring **entitlement windows** (Phase 4) — a trailing-5h and trailing-7d
     credit allowance that every user gets (even with no paid subscription, via
     the ``free`` tier). Exhausting a window falls through to prepaid balance.
     The allowance numbers live in ``SUBSCRIPTION_TIER_LIMITS`` (env), not here.
  2. **Provider plan IDs** — the per-provider identifiers needed to open a
     subscription checkout. Keyed by provider id so the same tier can be sold
     through Revolut today and another fiat provider tomorrow without touching
     the manager.

Pricing: Go $8 / Plus $20 / Max $100 per month (EUR plans use the same number,
VAT-inclusive — the price IS the gross total and Revolut breaks the VAT out within it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache

from src.config import config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

DEFAULT_TIER = "free"
DEFAULT_CURRENCY = "USD"

# Product namespaces for the tier registry. Series stay LTAI (libertai) / LCLW (liberclaw).
PRODUCT_LIBERTAI = "libertai"
PRODUCT_LIBERCLAW = "liberclaw"


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


_TIER_NAMES = ("free", "go", "plus", "max")


def _limits_error(detail: str) -> RuntimeError:
    return RuntimeError(f"SUBSCRIPTION_TIER_LIMITS {detail}")


def _load_tier_limits() -> dict[str, tuple[float, float]]:
    """Parse ``SUBSCRIPTION_TIER_LIMITS`` into {tier: (window_5h, weekly)}.

    Raises at import so a deployment missing (or mistyping) the variable fails to
    start rather than silently serving wrong allowances.
    """
    raw = config.SUBSCRIPTION_TIER_LIMITS
    if not raw:
        raise _limits_error('is not set; expected {"<tier>": {"window_5h": <n>, "weekly": <n>}, ...}')
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise _limits_error(f"is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise _limits_error("must be a JSON object keyed by tier name")

    limits: dict[str, tuple[float, float]] = {}
    for name in _TIER_NAMES:
        entry = parsed.get(name)
        if not isinstance(entry, dict):
            raise _limits_error(f"is missing tier {name!r}")
        values: list[float] = []
        for key in ("window_5h", "weekly"):
            value = entry.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise _limits_error(f"[{name!r}][{key!r}] must be a non-negative number")
            values.append(float(value))
        limits[name] = (values[0], values[1])
    return limits


_LIMITS = _load_tier_limits()


SUBSCRIPTION_TIERS: dict[str, TierConfig] = {
    "free": TierConfig(
        name="free",
        price_cents=0,
        currency=DEFAULT_CURRENCY,
        window_5h_credits=_LIMITS["free"][0],
        weekly_credits=_LIMITS["free"][1],
        provider_plan_ids={},
    ),
    "go": TierConfig(
        name="go",
        price_cents=800,
        currency=DEFAULT_CURRENCY,
        window_5h_credits=_LIMITS["go"][0],
        weekly_credits=_LIMITS["go"][1],
        provider_plan_ids={
            # One Revolut plan per tier; currency is a plan VARIATION (EUR variation is VAT-inclusive: 20% VAT within the price).
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
        window_5h_credits=_LIMITS["plus"][0],
        weekly_credits=_LIMITS["plus"][1],
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
        window_5h_credits=_LIMITS["max"][0],
        weekly_credits=_LIMITS["max"][1],
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

LIBERCLAW_SUBSCRIPTION_TIERS: dict[str, TierConfig] = {
    "free": TierConfig(
        name="free",
        price_cents=0,
        currency="EUR",
        window_5h_credits=0.0,
        weekly_credits=0.0,
        provider_plan_ids={},
    ),
    "starter": TierConfig(
        name="starter",
        price_cents=700,
        currency="EUR",
        window_5h_credits=0.0,
        weekly_credits=0.0,
        provider_plan_ids={
            "revolut": {
                "EUR": {
                    "plan_id": "a9a0b97f-753f-4e13-ac60-f86733809dce",
                    "variation_id": "88e34b68-abea-497a-9743-01874274dcdf",
                },
            }
        },
    ),
    "pro": TierConfig(
        name="pro",
        price_cents=1900,
        currency="EUR",
        window_5h_credits=0.0,
        weekly_credits=0.0,
        provider_plan_ids={
            "revolut": {
                "EUR": {
                    "plan_id": "c4c23aef-c39d-419d-99b6-f84034102615",
                    "variation_id": "2bdb31f1-78d5-48ad-88eb-c9c41fac57ef",
                },
            }
        },
    ),
    "team": TierConfig(
        name="team",
        price_cents=4900,
        currency="EUR",
        window_5h_credits=0.0,
        weekly_credits=0.0,
        provider_plan_ids={
            "revolut": {
                "EUR": {
                    "plan_id": "d66f42c8-5b08-4dc0-9bd1-8f17f3f70b7b",
                    "variation_id": "71a36c44-4277-495d-9258-6eba1c325559",
                },
            }
        },
    ),
}

# Product -> tier name -> config. LiberClaw entries are billed in EUR only, unlike LTAI's USD/EUR plans.
TIERS: dict[str, dict[str, TierConfig]] = {
    PRODUCT_LIBERTAI: SUBSCRIPTION_TIERS,
    PRODUCT_LIBERCLAW: LIBERCLAW_SUBSCRIPTION_TIERS,
}
DEFAULT_TIERS: dict[str, str] = {product: "free" for product in TIERS}


def _tiers_for(product: str) -> dict[str, TierConfig]:
    # Reads the SUBSCRIPTION_TIERS *name* (not TIERS[PRODUCT_LIBERTAI]) so tests that
    # monkeypatch module-level SUBSCRIPTION_TIERS keep working for the default product.
    if product == PRODUCT_LIBERTAI:
        return SUBSCRIPTION_TIERS
    return TIERS.get(product, {})


def get_tier(tier: str, product: str = PRODUCT_LIBERTAI) -> TierConfig:
    cfg = _tiers_for(product).get(tier)
    if cfg is None:
        raise ValueError(f"Unknown tier: {tier}")
    return cfg


def tier_order(product: str = PRODUCT_LIBERTAI) -> dict[str, int]:
    return {name: i for i, name in enumerate(_tiers_for(product))}


def paid_tiers(product: str = PRODUCT_LIBERTAI) -> set[str]:
    return {name for name, cfg in _tiers_for(product).items() if cfg.is_paid}


@lru_cache(maxsize=1)
def _revolut_plan_overrides() -> dict:
    """Env override for the Revolut plan ids (``REVOLUT_PLAN_IDS``, JSON).

    Lets an environment point at a different Revolut merchant environment than the
    in-code production ids — e.g. beta runs against the SANDBOX, whose plans have
    their own ids. Shape: {"libertai": {"go": {"USD": {...}}}, "liberclaw": {"starter": {...}}}.
    A top-level key matching an LTAI tier name is the legacy (pre-product) shape —
    beta env compatibility — and is treated as {"libertai": <raw>}.
    Tiers/currencies absent from the override fall back to the in-code ids.
    """
    raw = config.REVOLUT_PLAN_IDS
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("REVOLUT_PLAN_IDS is not valid JSON; ignoring the override")
        return {}
    if any(key in SUBSCRIPTION_TIERS for key in parsed):
        return {PRODUCT_LIBERTAI: parsed}
    return parsed


def get_provider_plan(tier: str, provider: str, currency: str, product: str = PRODUCT_LIBERTAI) -> dict[str, str]:
    """Return the {plan_id, variation_id} for a tier on a given provider in a given currency."""
    plan = get_tier(tier, product).provider_plan_ids.get(provider, {}).get(currency)
    if provider == "revolut":
        plan = _revolut_plan_overrides().get(product, {}).get(tier, {}).get(currency) or plan
    if not plan:
        raise ValueError(f"Tier {tier!r} is not sold through provider {provider!r} in currency {currency!r}")
    if any(value.startswith("TODO") for value in plan.values()):
        raise ValueError(
            f"Plan ids for tier {tier!r} on provider {provider!r} in currency {currency!r} are not configured"
        )
    return plan


def is_upgrade(current_tier: str, new_tier: str, product: str = PRODUCT_LIBERTAI) -> bool:
    order = tier_order(product)
    return order.get(new_tier, 0) > order.get(current_tier, 0)


def is_downgrade(current_tier: str, new_tier: str, product: str = PRODUCT_LIBERTAI) -> bool:
    order = tier_order(product)
    return order.get(new_tier, 0) < order.get(current_tier, 0)
