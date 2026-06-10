"""Fixed EUR top-up pack table lookups."""

import pytest

import src.topup_packs as topup_packs
from src.topup_packs import TOPUP_PACKS, get_pack


@pytest.fixture
def confirmed_packs(monkeypatch):
    """The pack table ships with placeholder amounts behind TOPUP_PACKS_CONFIRMED;
    tests of the lookup logic pretend the real table landed."""
    monkeypatch.setattr(topup_packs, "TOPUP_PACKS_CONFIRMED", True)


def test_get_pack_returns_known_pack(confirmed_packs):
    pack = get_pack("eur_10")
    assert pack.id == "eur_10"
    assert pack.usd_credits > 0
    assert pack.eur_charge > 0


def test_get_pack_unknown_raises_value_error(confirmed_packs):
    with pytest.raises(ValueError, match="nope"):
        get_pack("nope")


def test_get_pack_blocked_until_table_confirmed():
    """Placeholder amounts must never reach a real checkout: the guard mirrors the
    TODO plan-id guard in subscription_tiers.get_provider_plan."""
    with pytest.raises(ValueError, match="not configured"):
        get_pack("eur_10")


def test_all_packs_have_positive_amounts():
    for pack_id, pack in TOPUP_PACKS.items():
        assert pack.id == pack_id
        assert pack.usd_credits > 0
        assert pack.eur_charge > 0
