import pytest

from src.interfaces.aleph import (
    AlephAPIResponse,
    AudioCapability,
    AudioPricing,
    ModelInfo,
    ModelsResponse,
)
from src.services.aleph import aleph_service
from src.services.x402 import X402Service


@pytest.mark.asyncio
async def test_audio_price_exposed(monkeypatch):
    model = ModelInfo(
        id="kokoro",
        name="Kokoro 82M",
        capabilities={"audio": AudioCapability(languages=["en"], voices=["af_heart"])},
        pricing={"audio": AudioPricing(price_per_million_input_characters=0.70)},
    )
    fake = AlephAPIResponse(data={"LTAI_PRICING": ModelsResponse(models=[model], redirections=[])})
    monkeypatch.setattr(aleph_service, "models_data", fake)

    prices = await X402Service.get_current_prices()
    assert prices["kokoro"]["is_audio"] is True
    assert prices["kokoro"]["price_per_million_input_characters"] == 0.70
