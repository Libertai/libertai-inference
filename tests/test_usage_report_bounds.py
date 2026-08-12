"""Range invariants for the self-reported token counts on POST /api-keys/admin/usage.

The caller supplies its own token counts, so the numbers are untrusted input: a holder of a
valid key can put any int in the body. A negative count would subtract from the stats and
billing totals, so it is refused at the boundary. These tests pin that, and check that a
rejected report writes nothing at all — neither the chat-history row nor the metered call.
"""

import uuid

import pytest
from sqlalchemy import delete, func, select

from src.interfaces.credits import CreditTransactionProvider
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_email

pytestmark = pytest.mark.asyncio


async def _fake_calculate_price(**_kwargs) -> float:
    return 1.0


async def _seed_user_with_api_key(email: str):
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, email)
        await db.commit()
        user_id = user.id

    await CreditService.add_credits_for_user(user_id, 10.0, CreditTransactionProvider.voucher)
    api_key = await ApiKeyService.create_api_key(user_id=user_id, name="usage-bounds", user_address=None)
    return user_id, api_key


async def _row_counts(api_key_id) -> tuple[int, int]:
    async with AsyncSessionLocal() as db:
        calls = (
            await db.execute(
                select(func.count()).select_from(InferenceCall).where(InferenceCall.api_key_id == api_key_id)
            )
        ).scalar()
        chats = (
            await db.execute(select(func.count()).select_from(ChatRequest).where(ChatRequest.api_key_id == api_key_id))
        ).scalar()
    return int(calls or 0), int(chats or 0)


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"input_tokens": -1, "output_tokens": 10, "cached_tokens": 0}, id="negative-input"),
        pytest.param({"input_tokens": 10, "output_tokens": -1, "cached_tokens": 0}, id="negative-output"),
        pytest.param({"input_tokens": 10, "output_tokens": 10, "cached_tokens": -1}, id="negative-cached"),
        pytest.param(
            {"input_tokens": -1_000_000, "output_tokens": -1_000_000, "cached_tokens": 0}, id="both-negative"
        ),
    ],
)
async def test_negative_text_report_is_rejected_and_writes_nothing(monkeypatch, async_client, payload):
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    user_id, api_key = await _seed_user_with_api_key(f"usage-bounds-{uuid.uuid4().hex}@example.com")

    try:
        calls_before, chats_before = await _row_counts(api_key.id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={"key": api_key.full_key, "model_name": "test-text-model", **payload},
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"

        assert await _row_counts(api_key.id) == (calls_before, chats_before), (
            "A rejected usage report must not write a metered call or a chat-history row"
        )
    finally:
        await _cleanup(user_id)


async def test_zero_report_is_accepted(monkeypatch, async_client):
    """Zero is a valid count — the bound rejects only what is below it."""
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    user_id, api_key = await _seed_user_with_api_key(f"usage-bounds-ok-{uuid.uuid4().hex}@example.com")

    try:
        calls_before, _ = await _row_counts(api_key.id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": api_key.full_key,
                "model_name": "test-text-model",
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        calls_after, _ = await _row_counts(api_key.id)
        assert calls_after == calls_before + 1
    finally:
        await _cleanup(user_id)


async def test_negative_image_report_is_rejected(monkeypatch, async_client):
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    user_id, api_key = await _seed_user_with_api_key(f"usage-bounds-img-{uuid.uuid4().hex}@example.com")

    try:
        calls_before, chats_before = await _row_counts(api_key.id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": api_key.full_key,
                "model_name": "test-image-model",
                "image_count": -1,
                "type": "image",
            },
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
        assert await _row_counts(api_key.id) == (calls_before, chats_before)
    finally:
        await _cleanup(user_id)


async def test_negative_audio_report_is_rejected(monkeypatch, async_client):
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    user_id, api_key = await _seed_user_with_api_key(f"usage-bounds-audio-{uuid.uuid4().hex}@example.com")

    try:
        calls_before, chats_before = await _row_counts(api_key.id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": api_key.full_key,
                "model_name": "test-audio-model",
                "input_tokens": -5000,
                "type": "audio",
            },
        )
        assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
        assert await _row_counts(api_key.id) == (calls_before, chats_before)
    finally:
        await _cleanup(user_id)
