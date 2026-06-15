"""Fixed EUR top-up pack table lookups."""

import pytest

from src.topup_packs import TOPUP_PACKS, get_pack


def test_get_pack_returns_known_pack():
    pack = get_pack("eur_10")
    assert pack.id == "eur_10"
    assert pack.usd_credits > 0
    assert pack.eur_charge > 0


def test_get_pack_unknown_raises_value_error():
    with pytest.raises(ValueError, match="nope"):
        get_pack("nope")


def test_all_packs_have_positive_amounts():
    for pack_id, pack in TOPUP_PACKS.items():
        assert pack.id == pack_id
        assert pack.usd_credits > 0
        assert pack.eur_charge > 0
