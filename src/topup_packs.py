"""Preset top-up tiers.

Each tier credits a fixed USD value (``usd_credits``) to the prepaid balance. The charge
depends on the buyer's region: non-EU users pay the same USD value 1:1 (and may also enter an
arbitrary amount), while EU users pay ``eur_charge`` — a fixed GROSS EUR amount (VAT-inclusive /
TTC; the VAT portion is back-calculated and sent as an order line item). The frontend uses these
tiers as the preset amount choices for both currencies.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TopupPack:
    id: str
    usd_credits: float  # credited to the (USD-denominated) prepaid balance
    eur_charge: float  # gross EUR charged (VAT-inclusive / TTC; VAT back-calculated for the invoice)


# TODO: placeholder 1:1 EUR amounts — replace with the real EUR<->credits table before prod.
TOPUP_PACKS: dict[str, TopupPack] = {
    "eur_10": TopupPack("eur_10", usd_credits=10.0, eur_charge=10.0),
    "eur_50": TopupPack("eur_50", usd_credits=50.0, eur_charge=50.0),
    "eur_100": TopupPack("eur_100", usd_credits=100.0, eur_charge=100.0),
    "eur_250": TopupPack("eur_250", usd_credits=250.0, eur_charge=250.0),
}


def get_pack(pack_id: str) -> TopupPack:
    pack = TOPUP_PACKS.get(pack_id)
    if pack is None:
        raise ValueError(f"Unknown top-up pack: {pack_id!r}")
    return pack
