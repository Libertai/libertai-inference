"""Route-level tests for the chat-key metering logic in POST /api-keys/admin/usage.

Per-user chat keys must now be metered (ChatRequest + InferenceCall, balance deducted).
The shared anonymous key (config.LIBERTAI_CHAT_API_KEY) must still write a ChatRequest
but MUST NOT write an InferenceCall and MUST NOT deduct from any balance.

Each test seeds its own rows and cleans them up; the async_client fixture is used to POST
to the endpoint exactly as the production gateway would.
"""

import pytest
from sqlalchemy import delete, func, select

from src.config import config
from src.interfaces.credits import CreditTransactionProvider
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.credit_transaction import CreditTransaction
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_email
from src.subscription_tiers import get_tier

pytestmark = pytest.mark.asyncio

# Drain enough to exhaust the free tier weekly window (derived so it tracks the tier cap).
_FREE_WINDOW_DRAIN = get_tier("free").weekly_credits + 1.0

# A fixed price returned by the monkeypatched aleph_service.
_FIXED_PRICE = 3.0


async def _fake_calculate_price(**_kwargs) -> float:
    return _FIXED_PRICE


async def _seed_user_with_chat_key(email: str, prepaid: float):
    """Create an email user, add prepaid credits, and mint a per-user chat key."""
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, email)
        await db.commit()
        user_id = user.id

    await CreditService.add_credits_for_user(user_id, prepaid, CreditTransactionProvider.voucher)
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=None)
    return user_id, chat_key


async def _balance(user_id) -> float:
    from src.interfaces.credits import CreditTransactionStatus

    async with AsyncSessionLocal() as db:
        total = (
            await db.execute(
                select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.status == CreditTransactionStatus.completed,
                )
            )
        ).scalar()
    return float(total or 0.0)


async def _inference_call_count(api_key_id) -> int:
    async with AsyncSessionLocal() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(InferenceCall).where(InferenceCall.api_key_id == api_key_id)
            )
        ).scalar()
    return int(count or 0)


async def _chat_request_count(api_key_id) -> int:
    async with AsyncSessionLocal() as db:
        count = (
            await db.execute(
                select(func.count()).select_from(ChatRequest).where(ChatRequest.api_key_id == api_key_id)
            )
        ).scalar()
    return int(count or 0)


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# ---------------------------------------------------------------------------
# (a) Per-user chat key: TEXT usage past the free weekly window deducts balance
# ---------------------------------------------------------------------------


async def test_per_user_chat_key_text_metered_after_window_exhausted(monkeypatch, async_client):
    """POST /api-keys/admin/usage for a per-user chat key:
    - usage past the free weekly window (2.0 credits) should deduct from prepaid
    - a ChatRequest row must be created
    - an InferenceCall row must be created
    """
    # Patch aleph_service on the route module so no network call occurs.
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    email = "chat-metered-text@example.com"
    user_id, chat_key = await _seed_user_with_chat_key(email, prepaid=10.0)
    key_id = chat_key.id

    try:
        initial_balance = await _balance(user_id)
        assert initial_balance == 10.0

        # First call: exhaust the free weekly window by calling register_inference_call
        # directly so the window is used up, then test the route for the deduction path.
        # The drain amount is derived from the tier so it tracks the cap if it changes.
        await ApiKeyService.register_inference_call(
            key=chat_key.full_key, credits_used=_FREE_WINDOW_DRAIN, model_name="seed-model"
        )
        balance_after_drain = await _balance(user_id)
        assert balance_after_drain < 10.0, "Window was exhausted, prepaid should have been charged"

        # Record counts before the route call.
        ic_before = await _inference_call_count(key_id)
        cr_before = await _chat_request_count(key_id)
        balance_before_route = await _balance(user_id)

        # POST to the route — the chat branch should: add ChatRequest + call register_inference_call.
        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": chat_key.full_key,
                "model_name": "test-text-model",
                "input_tokens": 100,
                "output_tokens": 200,
                "cached_tokens": 0,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # A ChatRequest row must have been created.
        assert await _chat_request_count(key_id) == cr_before + 1

        # An InferenceCall row must have been created (metering).
        assert await _inference_call_count(key_id) == ic_before + 1

        # Balance must have dropped by exactly the fixed price (overflow -> prepaid deduction).
        balance_after_route = await _balance(user_id)
        assert balance_before_route - balance_after_route == pytest.approx(_FIXED_PRICE)

    finally:
        await _cleanup(user_id)


# ---------------------------------------------------------------------------
# (a2) Per-user chat key: cached tokens are input-side, never subtracted from output
# ---------------------------------------------------------------------------


async def test_per_user_chat_key_cached_tokens_priced_as_input(monkeypatch, async_client):
    """Regression: cached_tokens is a subset of *input* tokens. Subtracting it from
    output_tokens made calculate_price return a negative price when cached > output,
    which the ``check_credits_used_non_negative`` constraint rejected on insert.
    """
    import src.routes.api_keys.api_keys as route_module

    captured: dict = {}

    async def _spy_calculate_price(**kwargs) -> float:
        captured.update(kwargs)
        return _FIXED_PRICE

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _spy_calculate_price)

    email = "chat-cached-tokens@example.com"
    user_id, chat_key = await _seed_user_with_chat_key(email, prepaid=10.0)
    key_id = chat_key.id

    try:
        ic_before = await _inference_call_count(key_id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": chat_key.full_key,
                "model_name": "test-text-model",
                "input_tokens": 6320,
                "output_tokens": 257,
                "cached_tokens": 6208,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        assert captured["input_tokens"] == 6320
        assert captured["output_tokens"] == 257, "output_tokens must not have cached_tokens subtracted"
        assert captured["cached_tokens"] == 6208, "cached_tokens must be forwarded for the cached-input rate"

        assert await _inference_call_count(key_id) == ic_before + 1

    finally:
        await _cleanup(user_id)


# ---------------------------------------------------------------------------
# (b) Shared key: ChatRequest written, no InferenceCall, no balance change
# ---------------------------------------------------------------------------


async def test_shared_chat_key_no_inference_call_no_deduction(monkeypatch, async_client):
    """POST /api-keys/admin/usage for the shared anonymous chat key:
    - MUST create a ChatRequest row
    - MUST NOT create an InferenceCall row
    - MUST NOT deduct from the user's prepaid balance
    """
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    email = "chat-shared-key@example.com"
    user_id, chat_key = await _seed_user_with_chat_key(email, prepaid=10.0)
    key_id = chat_key.id

    # Patch the shared key to point at this per-user chat key.
    monkeypatch.setattr(config, "LIBERTAI_CHAT_API_KEY", chat_key.full_key)

    try:
        initial_balance = await _balance(user_id)
        ic_before = await _inference_call_count(key_id)
        cr_before = await _chat_request_count(key_id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": chat_key.full_key,
                "model_name": "test-text-model",
                "input_tokens": 100,
                "output_tokens": 200,
                "cached_tokens": 0,
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # ChatRequest MUST be created.
        assert await _chat_request_count(key_id) == cr_before + 1

        # InferenceCall MUST NOT be created for the shared key.
        assert await _inference_call_count(key_id) == ic_before, (
            "Shared key must not write an InferenceCall row"
        )

        # Balance MUST NOT change.
        assert await _balance(user_id) == initial_balance, (
            "Shared key must not deduct from prepaid balance"
        )

    finally:
        await _cleanup(user_id)


# ---------------------------------------------------------------------------
# (c) Per-user chat key: IMAGE usage past the free window meters (InferenceCall + ChatRequest)
# ---------------------------------------------------------------------------


async def test_per_user_chat_key_image_metered_after_window_exhausted(monkeypatch, async_client):
    """POST an image usage log (ImageInferenceCallData shape) for a per-user chat key with the
    free window exhausted: an InferenceCall row (+1) and a ChatRequest row (+1) must be written."""
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    email = "chat-metered-image@example.com"
    user_id, chat_key = await _seed_user_with_chat_key(email, prepaid=10.0)
    key_id = chat_key.id

    try:
        # Exhaust the free weekly window so the image call's overflow meters to prepaid.
        await ApiKeyService.register_inference_call(
            key=chat_key.full_key, credits_used=_FREE_WINDOW_DRAIN, model_name="seed-model"
        )
        assert await _balance(user_id) < 10.0

        ic_before = await _inference_call_count(key_id)
        cr_before = await _chat_request_count(key_id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": chat_key.full_key,
                "model_name": "test-image-model",
                "image_count": 2,
                "type": "image",
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # Both a ChatRequest and an InferenceCall row must have been written.
        assert await _chat_request_count(key_id) == cr_before + 1
        assert await _inference_call_count(key_id) == ic_before + 1

    finally:
        await _cleanup(user_id)


# ---------------------------------------------------------------------------
# (d) Shared key, IMAGE usage: ChatRequest written, ZERO InferenceCall
# ---------------------------------------------------------------------------


async def test_shared_chat_key_image_no_inference_call(monkeypatch, async_client):
    """POST an image usage log for the shared anonymous chat key: a ChatRequest row (+1) must be
    written but NO InferenceCall row."""
    import src.routes.api_keys.api_keys as route_module

    monkeypatch.setattr(route_module.aleph_service, "calculate_price", _fake_calculate_price)

    email = "chat-shared-image@example.com"
    user_id, chat_key = await _seed_user_with_chat_key(email, prepaid=10.0)
    key_id = chat_key.id

    monkeypatch.setattr(config, "LIBERTAI_CHAT_API_KEY", chat_key.full_key)

    try:
        ic_before = await _inference_call_count(key_id)
        cr_before = await _chat_request_count(key_id)

        resp = await async_client.post(
            "/api-keys/admin/usage",
            json={
                "key": chat_key.full_key,
                "model_name": "test-image-model",
                "image_count": 2,
                "type": "image",
            },
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        # ChatRequest MUST be created; InferenceCall MUST NOT.
        assert await _chat_request_count(key_id) == cr_before + 1
        assert await _inference_call_count(key_id) == ic_before, (
            "Shared key must not write an InferenceCall row (image path)"
        )

    finally:
        await _cleanup(user_id)
