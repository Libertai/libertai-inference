from src.interfaces.aleph import AudioCapability, AudioPricing, ModelInfo


def test_audio_pricing_parses():
    p = AudioPricing(price_per_million_input_characters=0.70)
    assert p.price_per_million_input_characters == 0.70


def test_model_info_accepts_audio_modality():
    model = ModelInfo(
        id="kokoro",
        name="Kokoro 82M",
        hf_id="hexgrad/Kokoro-82M",
        capabilities={"audio": AudioCapability(languages=["en", "fr"], voices=["af_heart"])},
        pricing={"audio": AudioPricing(price_per_million_input_characters=0.70)},
    )
    assert isinstance(model.pricing["audio"], AudioPricing)
    assert model.capabilities["audio"].voices == ["af_heart"]
