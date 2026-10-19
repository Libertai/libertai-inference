"""The pricing-aggregate URL and its config: composed live, checksum-validated at boot.

The aggregate moved once already (see PR #98) — these pin the two properties that keep
the next move cheap: the URL reflects config at fetch time (not import time), and a
typo'd mixed-case address override fails fast with a clear message.
"""

import pytest

from src.config import config, normalize_eth_address
from src.services.aleph import AlephService

_NEW_AGGREGATE = "0x238224C744F4b90b4494516e074D2676ECfC6803"


def test_api_url_composed_from_config():
    url = AlephService._api_url()
    assert url == f"https://api2.aleph.im/api/v0/aggregates/{_NEW_AGGREGATE}.json?keys=LTAI_PRICING"


def test_api_url_reflects_config_changes_at_runtime(monkeypatch):
    """The URL must not be frozen at import: an env override (or a test) takes effect."""
    monkeypatch.setattr(config, "ALEPH_API_URL", "https://aleph.example")
    monkeypatch.setattr(config, "ALEPH_PRICING_AGGREGATE_ADDRESS", "0x" + "ab" * 20)
    url = AlephService._api_url()
    assert url == f"https://aleph.example/api/v0/aggregates/0x{'ab' * 20}.json?keys=LTAI_PRICING"


def test_default_aggregate_address_is_checksummed():
    assert config.ALEPH_PRICING_AGGREGATE_ADDRESS == _NEW_AGGREGATE


def test_lowercase_address_normalized():
    assert normalize_eth_address("X", _NEW_AGGREGATE.lower()) == _NEW_AGGREGATE


def test_mixed_case_address_passing_checksum_accepted():
    assert normalize_eth_address("X", _NEW_AGGREGATE) == _NEW_AGGREGATE


def test_mixed_case_address_failing_checksum_rejected():
    # One flipped case makes the checksum fail: a typo'd override must fail fast.
    typo = _NEW_AGGREGATE.replace("0x238224C744", "0x238224c744")
    assert typo != typo.lower()  # still mixed-case, so the checksum applies
    with pytest.raises(RuntimeError, match="not a valid EIP-55 checksummed address"):
        normalize_eth_address("ALEPH_PRICING_AGGREGATE_ADDRESS", typo)
