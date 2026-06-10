"""Tests for geo-based currency resolution (EU -> EUR, everything else -> USD)."""

from types import SimpleNamespace

from src.services import geo


def _stub_request(headers: dict | None = None, client_host: str | None = None):
    headers = headers or {}
    client = SimpleNamespace(host=client_host) if client_host is not None else None
    return SimpleNamespace(headers=headers, client=client)


def _stub_reader(iso_code: str | None = None, exc: Exception | None = None):
    class Reader:
        def country(self, ip: str):
            if exc is not None:
                raise exc
            return SimpleNamespace(country=SimpleNamespace(iso_code=iso_code))

    return Reader()


# --- client_ip ---


def test_client_ip_uses_leftmost_x_forwarded_for():
    request = _stub_request(headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, client_host="9.9.9.9")
    assert geo.client_ip(request) == "1.2.3.4"


def test_client_ip_falls_back_to_request_client_host():
    request = _stub_request(client_host="9.9.9.9")
    assert geo.client_ip(request) == "9.9.9.9"


def test_client_ip_none_when_no_header_and_no_client():
    request = _stub_request()
    assert geo.client_ip(request) is None


# --- resolve_currency ---


def test_resolve_currency_eur_for_eu_country(monkeypatch):
    monkeypatch.setattr(geo, "_get_reader", lambda: _stub_reader(iso_code="FR"))
    request = _stub_request(headers={"x-forwarded-for": "1.2.3.4"})
    assert geo.resolve_currency(request) == "EUR"


def test_resolve_currency_usd_for_non_eu_country(monkeypatch):
    monkeypatch.setattr(geo, "_get_reader", lambda: _stub_reader(iso_code="US"))
    request = _stub_request(headers={"x-forwarded-for": "1.2.3.4"})
    assert geo.resolve_currency(request) == "USD"


def test_resolve_currency_usd_when_lookup_raises(monkeypatch):
    monkeypatch.setattr(geo, "_get_reader", lambda: _stub_reader(exc=ValueError("address not found")))
    request = _stub_request(headers={"x-forwarded-for": "10.0.0.1"})
    assert geo.resolve_currency(request) == "USD"


def test_resolve_currency_usd_when_reader_unavailable(monkeypatch):
    def _raise():
        raise FileNotFoundError("GeoLite2-Country.mmdb missing")

    monkeypatch.setattr(geo, "_get_reader", _raise)
    request = _stub_request(headers={"x-forwarded-for": "1.2.3.4"})
    assert geo.resolve_currency(request) == "USD"


def test_resolve_currency_usd_when_no_client_ip():
    request = _stub_request()
    assert geo.resolve_currency(request) == "USD"
