"""Fixed EUR top-up packs for EU users.

EU users buy a fixed GROSS EUR amount (VAT-inclusive; VAT is configured at the
Revolut merchant level — we never send a VAT field) and receive a fixed
USD-denominated credit. Non-EU users top up arbitrary USD amounts 1:1.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TopupPack:
    id: str
    usd_credits: float  # credited to the (USD-denominated) prepaid balance
    eur_charge: float  # gross EUR charged (VAT-inclusive)


# TODO: placeholder 1:1 amounts — confirm the real EUR<->credits table, then flip
# TOPUP_PACKS_CONFIRMED to True to enable EUR pack purchases (mirrors the TODO guard
# on EUR subscription plan ids in subscription_tiers.get_provider_plan).
TOPUP_PACKS_CONFIRMED = False

TOPUP_PACKS: dict[str, TopupPack] = {
    "eur_10": TopupPack("eur_10", usd_credits=10.0, eur_charge=10.0),
    "eur_25": TopupPack("eur_25", usd_credits=25.0, eur_charge=25.0),
    "eur_50": TopupPack("eur_50", usd_credits=50.0, eur_charge=50.0),
    "eur_100": TopupPack("eur_100", usd_credits=100.0, eur_charge=100.0),
}


def get_pack(pack_id: str) -> TopupPack:
    if not TOPUP_PACKS_CONFIRMED:
        raise ValueError("EUR top-up packs are not configured yet")
    pack = TOPUP_PACKS.get(pack_id)
    if pack is None:
        raise ValueError(f"Unknown top-up pack: {pack_id!r}")
    return pack
