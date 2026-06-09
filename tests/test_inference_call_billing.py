"""Billing rules for register_inference_call by key type.

Per-user chat keys are now chargeable like api/cli keys. The shared anonymous service
key (config.LIBERTAI_CHAT_API_KEY) stays free forever so logged-out chat is never broken.
These call the real service (own AsyncSessionLocal bound to the test DB); each test uses
a unique address so committed rows don't collide.
"""

from src.config import config
from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_email, get_or_create_user_by_wallet


async def _seed_user_with_credits(address: str, amount: float):
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id
    await CreditService.add_credits_for_user(user_id, amount, CreditTransactionProvider.voucher)
    return user_id


async def _balance(user_id) -> float:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        return await user.get_credit_balance()


async def test_per_user_chat_window_then_overflow_to_prepaid():
    """A per-user chat key is covered by the free tier window until it's
    exhausted, then overflow draws from prepaid. This proves the window logic actually engages
    (the key isn't silently free)."""
    address = "0xC4A7000000000000000000000000000000000013"
    user_id = await _seed_user_with_credits(address, 10.0)
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)

    # First call stays within the free window (5h cap 0.5, weekly cap 2.0): covered by tier.
    ok = await ApiKeyService.register_inference_call(
        key=chat_key.full_key, credits_used=0.4, model_name="test-model"
    )
    assert ok is True
    assert await _balance(user_id) == 10.0  # within free window — not charged

    # Second call pushes cumulative usage past the free window -> overflow draws from prepaid.
    ok = await ApiKeyService.register_inference_call(
        key=chat_key.full_key, credits_used=2.0, model_name="test-model"
    )
    assert ok is True
    assert await _balance(user_id) < 10.0  # overflow charged to prepaid


async def test_shared_free_chat_key_never_deducts(monkeypatch):
    """The anonymous service key (config.LIBERTAI_CHAT_API_KEY) stays free even once the free
    window is exhausted (source != "tier"), where a normal key WOULD deduct. This makes the
    is_shared_free_key guard the thing under test: removing it would fail this test."""
    address = "0xC4A7000000000000000000000000000000000014"
    user_id = await _seed_user_with_credits(address, 10.0)

    # Exhaust the free window via a normal (non-shared) api key so the next call's source
    # is no longer "tier" — i.e. a per-user key in this state would now draw from prepaid.
    api_key = await ApiKeyService.create_api_key(user_id=user_id, name="primer", user_address=address)
    ok = await ApiKeyService.register_inference_call(
        key=api_key.full_key, credits_used=2.5, model_name="test-model"  # > free weekly cap 2.0
    )
    assert ok is True
    balance_after_priming = await _balance(user_id)
    assert balance_after_priming < 10.0  # confirms a normal key deducts once past the window

    # Now route a call through the shared free key: it must NOT deduct despite source != "tier".
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)
    monkeypatch.setattr(config, "LIBERTAI_CHAT_API_KEY", chat_key.full_key)
    ok = await ApiKeyService.register_inference_call(
        key=chat_key.full_key, credits_used=5.0, model_name="test-model"
    )
    assert ok is True
    assert await _balance(user_id) == balance_after_priming  # shared free key is never charged


async def test_api_key_usage_beyond_free_window_deducts():
    """Contrast: a standard api key DOES draw down the balance once usage exceeds the free
    weekly window (2.0). A single 3.0-credit call overflows the window, so the overflow is
    charged to prepaid."""
    address = "0xA9100000000000000000000000000000000000011"
    user_id = await _seed_user_with_credits(address, 10.0)

    api_key = await ApiKeyService.create_api_key(user_id=user_id, name="std", user_address=address)

    ok = await ApiKeyService.register_inference_call(
        key=api_key.full_key, credits_used=3.0, model_name="test-model"
    )

    assert ok is True
    assert await _balance(user_id) == 7.0


async def test_chat_key_whitelisted_at_gateway_with_zero_balance():
    """The gateway invariant that makes chat free: a chat key for a user with NO credits must still
    appear in the admin whitelist (free tier window covers them). A soft-deleted one must not."""
    # A user with zero usage is within their free window -> whitelisted.
    address = "0xC4A7000000000000000000000000000000000012"
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id

    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)

    whitelist = await ApiKeyService.get_admin_all_api_keys()
    assert chat_key.full_key in whitelist  # present: zero usage is within free tier window

    await ApiKeyService.delete_api_key(chat_key.id)
    whitelist_after = await ApiKeyService.get_admin_all_api_keys()
    assert chat_key.full_key not in whitelist_after  # soft-deleted keys drop off


async def test_chat_api_key_for_email_user():
    """Email/OAuth users have no wallet address — the chat key path (the whole point of this fix)
    must still mint a valid chat key for them."""
    async with AsyncSessionLocal() as db:
        user, _created = await get_or_create_user_by_email(db, "chat-free@example.com")
        await db.commit()
        user_id = user.id
        user_address = user.address  # None for email users

    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=user_address)

    assert chat_key.type == ApiKeyType.chat
    assert isinstance(chat_key.full_key, str) and len(chat_key.full_key) > 0


async def test_blocked_chat_key_drops_from_whitelist():
    """A chat user who exhausted the free window AND has no prepaid is dropped."""
    address = "0xC4A7000000000000000000000000000000000015"
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)

    # Drain past the free weekly window (2.0) with zero prepaid -> blocked.
    await ApiKeyService.register_inference_call(
        key=chat_key.full_key, credits_used=2.5, model_name="m"
    )
    whitelist = await ApiKeyService.get_admin_all_api_keys()
    assert chat_key.full_key not in whitelist


async def test_shared_free_chat_key_always_whitelisted(monkeypatch):
    address = "0xC4A7000000000000000000000000000000000016"
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)
    monkeypatch.setattr(config, "LIBERTAI_CHAT_API_KEY", chat_key.full_key)
    # Even with usage that would block a normal user:
    await ApiKeyService.register_inference_call(key=chat_key.full_key, credits_used=5.0, model_name="m")

    whitelist = await ApiKeyService.get_admin_all_api_keys()
    assert chat_key.full_key in whitelist
