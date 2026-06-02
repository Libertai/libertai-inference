"""Crypto payment providers — first-class registry entries for wallet users.

These exist so the abstraction is genuinely provider-agnostic: a wallet user is
offered the provider matching their chain (``thirdweb`` for EVM wallets,
``solana`` for SOL wallets), exactly as a fiat user is offered Revolut.

Top-ups settle **on-chain**: the console sends funds to the descriptor's
``contract_address`` and the existing watchers (``routes/credits/ltai.py``,
``routes/credits/thirdweb.py``, ``services/solana.py``) credit the user. So
``create_topup`` is intentionally not implemented here — the descriptor carries
everything the client needs to build the on-chain transaction. Subscriptions are
fiat-only and unsupported on these providers.
"""

from __future__ import annotations

from src.services.payments.base import (
    PaymentCapability,
    PaymentProvider,
    PaymentProviderKind,
    ProviderDescriptor,
)


class ThirdwebPaymentProvider(PaymentProvider):
    """EVM (Base) wallet top-ups via the LTAI payment-processor contract / thirdweb pay."""

    def __init__(self, contract_address: str | None):
        self._contract_address = contract_address

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="thirdweb",
            kind=PaymentProviderKind.crypto,
            label="Crypto (Ethereum / Base)",
            capabilities=[PaymentCapability.topup],
            currencies=["USDC", "LTAI", "ETH"],
            chain="base",
            contract_address=self._contract_address,
            enabled=bool(self._contract_address),
        )


class SolanaPaymentProvider(PaymentProvider):
    """Solana wallet top-ups via the LTAI payment-processor program."""

    def __init__(self, contract_address: str | None):
        self._contract_address = contract_address

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="solana",
            kind=PaymentProviderKind.crypto,
            label="Crypto (Solana)",
            capabilities=[PaymentCapability.topup],
            currencies=["LTAI", "SOL"],
            chain="solana",
            contract_address=self._contract_address,
            enabled=bool(self._contract_address),
        )
