import pytest

from src.interfaces.aleph import AudioCapability, AudioPricing, ModelInfo
from src.services.aleph import aleph_service


@pytest.fixture
def kokoro_model(monkeypatch):
    model = ModelInfo(
        id="kokoro",
        name="Kokoro 82M",
        capabilities={"audio": AudioCapability(languages=["en"], voices=["af_heart"])},
        pricing={"audio": AudioPricing(price_per_million_input_characters=0.70)},
    )

    async def fake_get_model_info(model_id: str):
        return model if model_id == "kokoro" else None

    monkeypatch.setattr(aleph_service, "get_model_info", fake_get_model_info)
    return model


@pytest.mark.asyncio
async def test_audio_price_is_chars_times_rate(kokoro_model):
    # 1,000,000 chars at $0.70 / 1M chars == 0.70
    price = await aleph_service.calculate_price(model_id="kokoro", input_tokens=1_000_000)
    assert price == 0.70


@pytest.mark.asyncio
async def test_audio_price_small_input(kokoro_model):
    price = await aleph_service.calculate_price(model_id="kokoro", input_tokens=100)
    assert price == round(100 / 1_000_000 * 0.70, 5)


@pytest.mark.asyncio
async def test_audio_model_rejects_images(kokoro_model):
    with pytest.raises(ValueError):
        await aleph_service.calculate_price(model_id="kokoro", image_count=1)
