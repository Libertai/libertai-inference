"""Provider-agnostic payments.

The same abstraction covers every way a user can pay LibertAI:

  * **fiat** providers (Revolut, …) — hosted checkout + signed webhooks; support
    one-off top-ups *and* recurring subscriptions.
  * **crypto** providers (thirdweb for EVM wallets, Solana for SOL wallets) —
    settled on-chain; top-ups only. Their settlement currently lives in the
    on-chain webhook/poller (``routes/credits/*``); here they are first-class
    registry entries so the console can discover which payment methods apply to
    a given user (fiat for everyone, the matching chain provider for wallet
    users) and so future settlement can flow through the same ``PaymentEvent``.

Nothing in the manager or routes knows about a concrete provider — they speak
only ``PaymentProvider`` / ``PaymentEvent`` / ``ProviderDescriptor``.
"""

from src.services.payments.base import (
    CheckoutResult,
    PaymentCapability,
    PaymentEvent,
    PaymentEventType,
    PaymentProvider,
    PaymentProviderKind,
    ProviderDescriptor,
    SubscriptionInfo,
    UnsupportedCapability,
)
from src.services.payments.registry import payment_registry

__all__ = [
    "CheckoutResult",
    "PaymentCapability",
    "PaymentEvent",
    "PaymentEventType",
    "PaymentProvider",
    "PaymentProviderKind",
    "ProviderDescriptor",
    "SubscriptionInfo",
    "UnsupportedCapability",
    "payment_registry",
]
