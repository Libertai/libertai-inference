"""Regression: the kokoro instance reports audio usage with input_tokens only (char count,
no output_tokens) and type="audio". The /api-keys/admin/usage payload union must accept it
— it used to 422 because neither TextInferenceCallData (output_tokens required) nor
ImageInferenceCallData (image_count required) matched.
"""

import pytest
from pydantic import TypeAdapter

from src.interfaces.aleph import AudioCapability, AudioPricing, ModelInfo
from src.interfaces.api_keys import (
    AudioInferenceCallData,
    InferenceCallData,
    TextInferenceCallData,
)
from src.interfaces.credits import CreditTransactionProvider
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.aleph import aleph_service
from src.services.api_key import ApiKeyService
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_wallet


# Exactly what libertai-models' report_usage_event_task sends for a TTS call
# (AudioUsageFullData.model_dump()): no output_tokens, no cached_tokens.
def _instance_audio_payload(key: str, chars: int = 500_000) -> dict:
    return {
        "key": key,
        "model_name": "kokoro-82m",
        "endpoint": "v1/audio/speech",
        "payment_payload": None,
        "payment_requirements": None,
        "input_tokens": chars,
        "type": "audio",
    }


def test_audio_payload_validates_against_union():
    parsed = TypeAdapter(InferenceCallData).validate_python(_instance_audio_payload("ltai_x"))
    assert isinstance(parsed, AudioInferenceCallData)
    assert parsed.input_tokens == 500_000
    assert parsed.output_tokens == 0
    assert parsed.cached_tokens == 0


def test_text_payload_still_resolves_to_text_arm():
    # Guard against union ambiguity: a normal text report (type omitted) must not be
    # swallowed by the audio arm and lose its output_tokens.
    payload = {
        "key": "ltai_x",
        "model_name": "hermes-3-8b-tee",
        "input_tokens": 10,
        "output_tokens": 20,
        "cached_tokens": 0,
    }
    parsed = TypeAdapter(InferenceCallData).validate_python(payload)
    assert isinstance(parsed, TextInferenceCallData)
    assert parsed.output_tokens == 20


@pytest.fixture
def kokoro_model(monkeypatch):
    model = ModelInfo(
        id="kokoro-82m",
        name="Kokoro 82M",
        capabilities={"audio": AudioCapability(languages=["en"], voices=["af_heart"])},
        pricing={"audio": AudioPricing(price_per_million_input_characters=0.70)},
    )

    async def fake_get_model_info(model_id: str):
        return model if model_id == "kokoro-82m" else None

    monkeypatch.setattr(aleph_service, "get_model_info", fake_get_model_info)
    return model


@pytest.mark.asyncio
async def test_audio_usage_endpoint_bills_characters(async_client, kokoro_model):
    """Audio usage bills characters like any chargeable call: the free-tier window covers
    the first 0.5 credits (5h cap), the overflow draws from prepaid. 5M chars * $0.70/1M
    = $3.50 -> 0.5 tier-covered, 3.0 charged -> balance 10.0 - 3.0 = 7.0."""
    address = "0xA0D10000000000000000000000000000000000001"
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id
    await CreditService.add_credits_for_user(user_id, 10.0, CreditTransactionProvider.voucher)

    api_key = await ApiKeyService.create_api_key(user_id=user_id, name="tts", user_address=address)

    response = await async_client.post(
        "/api-keys/admin/usage", json=_instance_audio_payload(api_key.full_key, chars=5_000_000)
    )

    assert response.status_code == 200, response.text
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        balance = await user.get_credit_balance()
    assert balance == pytest.approx(7.0)  # $3.50 cost - 0.5 free-window = 3.0 from prepaid
