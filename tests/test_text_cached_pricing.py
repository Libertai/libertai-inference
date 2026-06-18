import pytest

from src.interfaces.aleph import ModelInfo, TextCapability, TextPricing
from src.services.aleph import aleph_service


def _make_model(monkeypatch, cached_rate):
    model = ModelInfo(
        id="glm",
        name="GLM",
        capabilities={"text": TextCapability(context_window=1000, function_calling=True, reasoning=True)},
        pricing={
            "text": TextPricing(
                price_per_million_input_tokens=10.0,
                price_per_million_output_tokens=30.0,
                price_per_million_cached_input_tokens=cached_rate,
            )
        },
    )

    async def fake_get_model_info(model_id: str):
        return model if model_id == "glm" else None

    monkeypatch.setattr(aleph_service, "get_model_info", fake_get_model_info)
    return model


@pytest.fixture
def model_with_cached_rate(monkeypatch):
    return _make_model(monkeypatch, cached_rate=1.0)


@pytest.fixture
def model_without_cached_rate(monkeypatch):
    return _make_model(monkeypatch, cached_rate=None)


@pytest.mark.asyncio
async def test_no_cached_tokens_unchanged(model_with_cached_rate):
    # 1M input + 1M output, no cache: 10 + 30
    price = await aleph_service.calculate_price(
        model_id="glm", input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=0
    )
    assert price == 40.0


@pytest.mark.asyncio
async def test_cached_tokens_billed_at_cached_rate(model_with_cached_rate):
    # 600k full-rate input + 400k cached input + 0 output: 6.0 + 0.4
    price = await aleph_service.calculate_price(
        model_id="glm", input_tokens=1_000_000, output_tokens=0, cached_tokens=400_000
    )
    assert price == round(600_000 / 1_000_000 * 10.0 + 400_000 / 1_000_000 * 1.0, 5)
    assert price == 6.4


@pytest.mark.asyncio
async def test_cached_does_not_touch_output(model_with_cached_rate):
    # cached must reduce input, never output
    price = await aleph_service.calculate_price(
        model_id="glm", input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=1_000_000
    )
    # all input cached (1.0) + full output (30.0)
    assert price == round(1.0 + 30.0, 5)


@pytest.mark.asyncio
async def test_no_cached_rate_falls_back_to_input_rate(model_without_cached_rate):
    # cached rate unset -> cached billed at full input rate -> no discount
    price = await aleph_service.calculate_price(
        model_id="glm", input_tokens=1_000_000, output_tokens=0, cached_tokens=500_000
    )
    assert price == 10.0


@pytest.mark.asyncio
async def test_cached_clamped_to_input(model_with_cached_rate):
    # cached > input must not produce negative full-rate input
    price = await aleph_service.calculate_price(
        model_id="glm", input_tokens=100, output_tokens=0, cached_tokens=1000
    )
    # clamped to 100 cached tokens at cached rate 1.0
    assert price == round(100 / 1_000_000 * 1.0, 5)
