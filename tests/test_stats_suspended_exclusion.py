"""Suspended accounts are excluded from every global statistic.

A live user and a suspended user are given identical traffic, so any statistic that still
counts the suspended one shows up as a doubled figure rather than a subtle skew. An
ownerless key (``user_id`` NULL) carries traffic too: it has no account to suspend and must
survive the filter, which is what separates the outer join from an inner one.

Rows are committed through the real ``AsyncSessionLocal`` and stamped in a fixed 2021 window
queried exactly, so rows committed by other tests (stamped "now") never fall in range.
"""

from datetime import date, datetime

from sqlalchemy import select

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.stats import StatsService
from src.services.users import get_or_create_user_by_wallet

START = date(2021, 3, 1)
END = date(2021, 3, 2)
DAY1 = datetime(2021, 3, 1, 12, 0, 0)

LIVE = "0x5U5000000000000000000000000000000000L1VE"
BANNED = "0x5U5000000000000000000000000000000000BA00"

TOKENS_IN = 100
TOKENS_OUT = 20
CREDITS = 0.5


def _call(api_key_id, when: datetime) -> InferenceCall:
    call = InferenceCall(
        api_key_id=api_key_id,
        credits_used=CREDITS,
        model_name="suspended-test-model",
        input_tokens=TOKENS_IN,
        output_tokens=TOKENS_OUT,
        tier_credits_used=CREDITS,
    )
    call.used_at = when
    return call


def _chat(api_key_id, when: datetime) -> ChatRequest:
    cr = ChatRequest(
        api_key_id=api_key_id,
        input_tokens=TOKENS_IN,
        output_tokens=TOKENS_OUT,
        cached_tokens=0,
        model_name="suspended-test-model",
    )
    cr.created_at = when
    return cr


async def _seed() -> None:
    """One live and one suspended account with identical traffic, plus an ownerless key.

    Idempotent: every test in this module calls it, and the assertions are absolute counts
    over a shared window, so a second seed would double them.
    """
    async with AsyncSessionLocal() as db:
        already = (await db.execute(select(ApiKey).where(ApiKey.name == "susp-live-api"))).scalars().first()
        if already is not None:
            return

        live = await get_or_create_user_by_wallet(db, LIVE)
        banned = await get_or_create_user_by_wallet(db, BANNED)
        await db.flush()

        banned.suspended_at = datetime(2021, 3, 2, 0, 0, 0)
        banned.suspension_reason = "test fleet"

        live_api = ApiKey(key=ApiKey.generate_key(), name="susp-live-api", user_id=live.id, type=ApiKeyType.api)
        live_chat = ApiKey(key=ApiKey.generate_key(), name="susp-live-chat", user_id=live.id, type=ApiKeyType.chat)
        banned_api = ApiKey(key=ApiKey.generate_key(), name="susp-ban-api", user_id=banned.id, type=ApiKeyType.api)
        banned_chat = ApiKey(key=ApiKey.generate_key(), name="susp-ban-chat", user_id=banned.id, type=ApiKeyType.chat)
        # No owner to suspend; must survive the filter.
        orphan_api = ApiKey(key=ApiKey.generate_key(), name="susp-orphan-api", type=ApiKeyType.api)
        db.add_all([live_api, live_chat, banned_api, banned_chat, orphan_api])
        await db.flush()

        db.add_all(
            [
                _call(live_api.id, DAY1),
                _call(banned_api.id, DAY1),
                _call(orphan_api.id, DAY1),
                _chat(live_chat.id, DAY1),
                _chat(banned_chat.id, DAY1),
            ]
        )
        await db.commit()


async def test_inference_totals_exclude_suspended():
    await _seed()

    # Two api calls survive: the live user's and the ownerless key's.
    calls = await StatsService._get_inference_api_stats(ApiKeyType.api, START, END)
    assert calls.total_calls == 2

    tokens = await StatsService._get_inference_tokens_stats(ApiKeyType.api, START, END)
    assert tokens.total_input_tokens == 2 * TOKENS_IN
    assert tokens.total_output_tokens == 2 * TOKENS_OUT

    credits = await StatsService._get_inference_credits_stats(ApiKeyType.api, START, END)
    assert credits.total_credits_used == 2 * CREDITS


async def test_chat_totals_exclude_suspended():
    await _seed()

    calls = await StatsService.get_global_chat_calls_stats(START, END)
    assert calls.total_calls == 1

    tokens = await StatsService.get_global_chat_tokens_stats(START, END)
    assert tokens.total_input_tokens == TOKENS_IN
    assert tokens.total_output_tokens == TOKENS_OUT


async def test_summary_excludes_suspended():
    await _seed()

    summary = await StatsService.get_global_summary_stats(START, END)
    # 2 inference (live + ownerless) + 1 chat (live).
    assert summary.total_requests == 3
    assert summary.total_input_tokens == 3 * TOKENS_IN
    assert summary.total_output_tokens == 3 * TOKENS_OUT


async def test_active_users_exclude_suspended():
    await _seed()

    api = await StatsService._get_inference_users_stats(ApiKeyType.api, START, END)
    # The ownerless key has no identity, so only the live user is counted.
    assert api.total_unique_users == 1

    chat = await StatsService.get_global_chat_users_stats(START, END)
    assert chat.total_unique_users == 1

    agg = await StatsService.get_global_users_stats(START, END)
    assert agg.total_unique_users == 1
    assert {d.date: d.active_users for d in agg.daily_active_users} == {"2021-03-01": 1}


async def test_segments_and_credits_exclude_suspended():
    await _seed()

    messages = await StatsService.get_global_messages_by_segment(START, END)
    assert messages.total_messages == 1

    calls = await StatsService.get_global_calls_by_segment(ApiKeyType.api, START, END)
    assert calls.total_calls == 2  # live + ownerless

    # chat keys are chargeable, so the live user's chat call has no inference row here;
    # credits come from the two surviving api calls only.
    consumption = await StatsService.get_global_credits_consumption(START, END)
    assert consumption.total_credits == 2 * CREDITS

    activity = await StatsService.get_global_user_base_activity(START, END)
    assert activity.free_active_users == 1


async def test_user_base_counts_drop_suspended_accounts():
    """Counted as a delta: this endpoint has no date range, so other tests' rows are in scope."""
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, "0x5U5000000000000000000000000000000000C0UN")
        await db.flush()
        user.suspended_at = None
        await db.commit()

    before = await StatsService.get_global_subscriptions_stats()

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(User).where(User.address == "0x5U5000000000000000000000000000000000C0UN"))
        ).scalar_one()
        row.suspended_at = datetime(2021, 3, 2, 0, 0, 0)
        await db.commit()

    after = await StatsService.get_global_subscriptions_stats()
    assert after.free_users == before.free_users - 1
