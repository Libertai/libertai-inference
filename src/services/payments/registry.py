"""Payment provider registry — the single place that knows the concrete set of providers."""

from __future__ import annotations

from src.config import config
from src.services.payments.base import (
    PaymentCapability,
    PaymentProvider,
    PaymentProviderKind,
    ProviderDescriptor,
)
from src.services.payments.crypto import SolanaPaymentProvider, ThirdwebPaymentProvider
from src.services.payments.revolut import RevolutProvider


class PaymentRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, PaymentProvider] = {}

    def register(self, provider: PaymentProvider) -> None:
        self._providers[provider.id] = provider

    def get(self, provider_id: str) -> PaymentProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise KeyError(f"Unknown payment provider: {provider_id}")
        return provider

    def all(self) -> list[PaymentProvider]:
        return list(self._providers.values())

    def descriptors(self) -> list[ProviderDescriptor]:
        return [p.descriptor() for p in self._providers.values()]

    def with_capability(self, capability: PaymentCapability) -> list[PaymentProvider]:
        return [p for p in self._providers.values() if p.supports(capability)]

    def available_for_chains(self, chains: list[str]) -> list[ProviderDescriptor]:
        """Descriptors a user may use: all enabled fiat providers, plus the crypto
        providers matching the user's connected wallet chains."""
        result: list[ProviderDescriptor] = []
        for descriptor in self.descriptors():
            if not descriptor.enabled:
                continue
            if descriptor.kind == PaymentProviderKind.fiat:
                result.append(descriptor)
            elif descriptor.chain in chains:
                result.append(descriptor)
        return result


def build_registry() -> PaymentRegistry:
    registry = PaymentRegistry()
    registry.register(
        RevolutProvider(
            secret_key=config.REVOLUT_SECRET_KEY,
            webhook_secret=config.REVOLUT_WEBHOOK_SECRET,
            api_url=config.REVOLUT_API_URL,
            api_version=config.REVOLUT_API_VERSION,
        )
    )
    registry.register(ThirdwebPaymentProvider(contract_address=str(config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE)))
    registry.register(SolanaPaymentProvider(contract_address=str(config.LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA)))
    return registry


payment_registry = build_registry()
