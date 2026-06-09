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


async def test_per_user_chat_key_deducts_when_subscriptions_disabled(monkeypatch):
    """Subscriptions off: a per-user chat key now draws down prepaid (no longer free)."""
    monkeypatch.setattr(config, "SUBSCRIPTIONS_ENABLED", False)
    address = "0xC4A7000000000000000000000000000000000010"
    user_id = await _seed_user_with_credits(address, 10.0)
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)

    ok = await ApiKeyService.register_inference_call(
        key=chat_key.full_key, credits_used=3.0, model_name="test-model"
    )
    assert ok is True
    assert await _balance(user_id) == 7.0  # chat now metered


async def test_per_user_chat_within_free_window_does_not_deduct(monkeypatch):
    """Subscriptions on, usage within the free weekly window (2.0): covered by tier, no deduct."""
    monkeypatch.setattr(config, "SUBSCRIPTIONS_ENABLED", True)
    address = "0xC4A7000000000000000000000000000000000013"
    user_id = await _seed_user_with_credits(address, 10.0)
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)

    ok = await ApiKeyService.register_inference_call(
        key=chat_key.full_key, credits_used=0.4, model_name="test-model"
    )
    assert ok is True
    assert await _balance(user_id) == 10.0  # within free window — not charged


async def test_shared_free_chat_key_never_deducts(monkeypatch):
    """The anonymous service key (config.LIBERTAI_CHAT_API_KEY) stays free regardless of flag."""
    monkeypatch.setattr(config, "SUBSCRIPTIONS_ENABLED", True)
    address = "0xC4A7000000000000000000000000000000000014"
    user_id = await _seed_user_with_credits(address, 10.0)
    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)
    monkeypatch.setattr(config, "LIBERTAI_CHAT_API_KEY", chat_key.full_key)

    ok = await ApiKeyService.register_inference_call(
        key=chat_key.full_key, credits_used=5.0, model_name="test-model"
    )
    assert ok is True
    assert await _balance(user_id) == 10.0  # shared free key is never charged


async def test_api_key_usage_deducts_credits(monkeypatch):
    """Contrast: a standard api key DOES draw down the balance (subscriptions disabled)."""
    # Pin the gate-on-prepaid path explicitly rather than relying on the env default, so the
    # contrast holds regardless of a developer's local SUBSCRIPTIONS_ENABLED setting.
    monkeypatch.setattr(config, "SUBSCRIPTIONS_ENABLED", False)
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
    appear in the admin whitelist (chat keys bypass the balance gate). A soft-deleted one must not."""
    address = "0xC4A7000000000000000000000000000000000012"
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id

    chat_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user_id, user_address=address)

    whitelist = await ApiKeyService.get_admin_all_api_keys()
    assert chat_key.full_key in whitelist  # present despite zero balance

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
