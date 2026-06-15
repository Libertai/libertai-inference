"""Fixed EUR top-up packs for EU users.

EU users buy a fixed GROSS EUR amount (VAT-inclusive / TTC; the VAT portion is
back-calculated and sent as an order line item) and receive a fixed USD-denominated
credit. Non-EU users top up arbitrary USD amounts 1:1.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TopupPack:
    id: str
    usd_credits: float  # credited to the (USD-denominated) prepaid balance
    eur_charge: float  # gross EUR charged (VAT-inclusive / TTC; VAT back-calculated for the invoice)


# TODO: placeholder 1:1 amounts — replace with the real EUR<->credits table before prod.
TOPUP_PACKS: dict[str, TopupPack] = {
    "eur_10": TopupPack("eur_10", usd_credits=10.0, eur_charge=10.0),
    "eur_25": TopupPack("eur_25", usd_credits=25.0, eur_charge=25.0),
    "eur_50": TopupPack("eur_50", usd_credits=50.0, eur_charge=50.0),
    "eur_100": TopupPack("eur_100", usd_credits=100.0, eur_charge=100.0),
}


def get_pack(pack_id: str) -> TopupPack:
    pack = TOPUP_PACKS.get(pack_id)
    if pack is None:
        raise ValueError(f"Unknown top-up pack: {pack_id!r}")
    return pack
