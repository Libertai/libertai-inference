"""Payment provider abstraction — ABC, capability model, and shared dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from src.subscription_tiers import PRODUCT_LIBERTAI


class PaymentProviderKind(str, Enum):
    fiat = "fiat"
    crypto = "crypto"


class PaymentCapability(str, Enum):
    topup = "topup"  # one-off prepaid credit purchase
    subscription = "subscription"  # recurring tier billing


class PaymentEventType(str, Enum):
    """Normalized inbound event, mapped from each provider's own vocabulary."""

    order_completed = "order_completed"
    order_failed = "order_failed"
    subscription_initiated = "subscription_initiated"
    subscription_cancelled = "subscription_cancelled"
    subscription_overdue = "subscription_overdue"
    subscription_finished = "subscription_finished"
    ignored = "ignored"  # received but not actionable


@dataclass
class ProviderDescriptor:
    """Public, client-facing description of a provider (served by ``/payments/providers``)."""

    id: str
    kind: PaymentProviderKind
    label: str
    capabilities: list[PaymentCapability]
    currencies: list[str] = field(default_factory=list)
    chain: str | None = None  # crypto only: "base" | "solana"
    contract_address: str | None = None  # crypto only: on-chain payment processor
    enabled: bool = True  # creds configured / provider usable


@dataclass
class CheckoutResult:
    """Result of opening a hosted checkout (top-up or subscription)."""

    checkout_url: str
    provider_subscription_id: str | None = None
    provider_customer_id: str | None = None
    order_id: str | None = None


# Provider states meaning the subscription is real: money has moved, or the mandate is
# live. A local row backed by one of these was paid for, however unpaid it looks locally.
LIVE_PROVIDER_STATES = frozenset({"active", "overdue", "paused"})


@dataclass
class SubscriptionInfo:
    """Subscription status as reported by the provider."""

    provider_subscription_id: str
    state: str
    current_cycle_start: str | None = None
    current_cycle_end: str | None = None


@dataclass
class PaymentEvent:
    """A verified, normalized inbound event the manager can act on."""

    provider: str
    type: PaymentEventType
    provider_event_id: str  # dedup key
    provider_subscription_id: str | None = None
    order_id: str | None = None
    amount: float | None = None  # top-ups: amount in major currency units
    currency: str | None = None
    metadata: dict = field(default_factory=dict)


class UnsupportedCapability(Exception):
    """Raised when a provider is asked to do something it doesn't support."""


class PaymentProvider(ABC):
    """Abstract payment provider.

    A provider implements only the capabilities it declares in ``descriptor()``;
    the optional methods below raise :class:`UnsupportedCapability` by default.
    """

    @abstractmethod
    def descriptor(self) -> ProviderDescriptor: ...

    @property
    def id(self) -> str:
        return self.descriptor().id

    @property
    def kind(self) -> PaymentProviderKind:
        return self.descriptor().kind

    def supports(self, capability: PaymentCapability) -> bool:
        return capability in self.descriptor().capabilities

    # ---- top-ups (one-off) ----
    async def create_topup(
        self,
        *,
        amount: float,
        currency: str,
        redirect_url: str,
        user_email: str | None = None,
        metadata: dict | None = None,
        vat_rate: float = 0.0,
        item_name: str = "Prepaid credits",
    ) -> CheckoutResult:
        # vat_rate/item_name are honoured by fiat providers (Revolut) for VAT-inclusive invoicing;
        # crypto providers don't implement top-ups at all.
        raise UnsupportedCapability(f"{self.id} does not support top-ups")

    # ---- subscriptions (recurring) ----
    async def create_subscription(
        self,
        *,
        user_email: str,
        tier: str,
        currency: str,
        redirect_url: str,
        provider_customer_id: str | None = None,
        product: str = PRODUCT_LIBERTAI,
    ) -> CheckoutResult:
        raise UnsupportedCapability(f"{self.id} does not support subscriptions")

    async def cancel_subscription(self, provider_subscription_id: str) -> None:
        raise UnsupportedCapability(f"{self.id} does not support subscriptions")

    async def change_subscription_plan(
        self, provider_subscription_id: str, *, tier: str, currency: str, product: str = PRODUCT_LIBERTAI
    ) -> None:
        """Schedule a plan change (e.g. a downgrade) to take effect at the end of the
        current billing cycle. The next cycle bills the target tier's plan."""
        raise UnsupportedCapability(f"{self.id} does not support subscriptions")

    async def get_subscription(self, provider_subscription_id: str) -> SubscriptionInfo:
        raise UnsupportedCapability(f"{self.id} does not support subscriptions")

    async def missed_activation_event(self, provider_subscription_id: str) -> PaymentEvent | None:
        """The activation event this subscription should have sent, if it is paid and live.

        Lets a sweep recover a payment whose webhook was lost: providers retry a failed
        delivery only a few times before giving up for good, so redelivery is not a
        durable safety net. Returns None when the provider agrees nothing was paid.
        """
        raise UnsupportedCapability(f"{self.id} does not support subscriptions")

    async def get_cycle(self, provider_subscription_id: str, cycle_id: str) -> dict:
        raise UnsupportedCapability(f"{self.id} does not support subscriptions")

    async def get_current_cycle(self, provider_subscription_id: str) -> dict:
        """The subscription's current cycle (carries its ``order_id``), resolved from a raw
        subscription fetch's ``current_cycle_id``. Lets a caller resolve "the order currently
        being billed" from nothing but a subscription id (sweep/backfill path)."""
        raise UnsupportedCapability(f"{self.id} does not support subscriptions")

    async def get_order(self, order_id: str) -> dict:
        raise UnsupportedCapability(f"{self.id} does not expose orders")

    # ---- inbound settlement ----
    def parse_webhook(self, headers: dict, body: bytes) -> PaymentEvent:
        """Verify signature and normalize a webhook payload.

        Raises ``ValueError`` on an invalid/forged payload.
        """
        raise UnsupportedCapability(f"{self.id} has no webhook")
