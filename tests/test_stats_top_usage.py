"""Top-users-by-usage and paginated active-users stats.

Seeds rows through the real ``AsyncSessionLocal`` (committed) so ``StatsService`` — which
opens its own session — can see them, mirroring the other stats tests. Everything is
stamped in a fixed 2020 window and queried over exactly that range, so committed rows
from other tests (stamped "now") never fall in range and can't pollute the counts.
"""

from datetime import date, datetime

from sqlalchemy import select

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.inference_call import InferenceCall
from src.models.liberclaw_user import LiberclawUser
from src.services.stats import StatsService
from src.services.users import get_or_create_user_by_wallet

# 2020-02 window: distinct from test_stats_users.py's 2020-01 so neither suite's
# committed rows pollute the other's absolute counts.
START = date(2020, 2, 1)
END = date(2020, 2, 3)
DAY1 = datetime(2020, 2, 1, 12, 0, 0)
DAY2 = datetime(2020, 2, 2, 12, 0, 0)

U1 = "0xDA0100000000000000000000000000000000B001"
U2 = "0xDA0100000000000000000000000000000000B002"
SUSPENDED = "0xDA0100000000000000000000000000000000B003"

API_KEY_TAG = "top-usage-api-key"
CLI_KEY_TAG = "top-usage-cli-key"
CHAT_KEY_TAG = "top-usage-chat-key"
LC_TAG = "top-usage-lc"


def _inference_call(api_key_id, when: datetime, credits: float) -> InferenceCall:
    call = InferenceCall(api_key_id=api_key_id, credits_used=credits, model_name="test-model")
    call.used_at = when
    return call


def _chat_request(api_key_id, when: datetime) -> ChatRequest:
    cr = ChatRequest(api_key_id=api_key_id, input_tokens=1, output_tokens=1, cached_tokens=0, model_name="test-model")
    cr.created_at = when
    return cr


async def _seed() -> None:
    """Two account users (u1 heavy on api + chat, u2 light on api) + one liberclaw identity.

    u1 also owns a CLI key with a single call, so by-key grouping yields duplicate emails.

    Idempotent: every test in this module calls it, and the assertions are absolute counts
    over a shared window, so a second seed would double them.
    """
    async with AsyncSessionLocal() as db:
        already = (
            (await db.execute(select(ApiKey).where(ApiKey.name == f"{API_KEY_TAG}-1", ApiKey.type == ApiKeyType.api)))
            .scalars()
            .first()
        )
        if already is not None:
            return

        user1 = await get_or_create_user_by_wallet(db, U1)
        user2 = await get_or_create_user_by_wallet(db, U2)
        await db.flush()

        liberclaw_user = (
            await db.execute(
                select(LiberclawUser).where(LiberclawUser.user_id == LC_TAG, LiberclawUser.user_type == "telegram")
            )
        ).scalar_one_or_none()
        if liberclaw_user is None:
            liberclaw_user = LiberclawUser(user_id=LC_TAG, user_type="telegram")
            db.add(liberclaw_user)
            await db.flush()

        u1_api = ApiKey(key=ApiKey.generate_key(), name=f"{API_KEY_TAG}-1", user_id=user1.id, type=ApiKeyType.api)
        u1_cli = ApiKey(key=ApiKey.generate_key(), name=f"{CLI_KEY_TAG}-1", user_id=user1.id, type=ApiKeyType.cli)
        u1_chat = ApiKey(key=ApiKey.generate_key(), name=f"{CHAT_KEY_TAG}-1", user_id=user1.id, type=ApiKeyType.chat)
        u2_api = ApiKey(key=ApiKey.generate_key(), name=f"{API_KEY_TAG}-2", user_id=user2.id, type=ApiKeyType.api)
        lc_key = ApiKey(
            key=ApiKey.generate_key(),
            name=LC_TAG,
            liberclaw_user_id=liberclaw_user.id,
            type=ApiKeyType.liberclaw,
        )
        db.add_all([u1_api, u1_cli, u1_chat, u2_api, lc_key])
        await db.flush()

        db.add_all(
            [
                # u1: 3 api calls (9 credits) + 1 cli call (1 credit) + 2 chat requests
                _inference_call(u1_api.id, DAY1, 4.0),
                _inference_call(u1_api.id, DAY2, 5.0),
                _inference_call(u1_cli.id, DAY1, 1.0),
                _chat_request(u1_chat.id, DAY1),
                _chat_request(u1_chat.id, DAY2),
                # u2: 1 api call (2 credits)
                _inference_call(u2_api.id, DAY1, 2.0),
                # liberclaw: 2 calls (7 credits)
                _inference_call(lc_key.id, DAY1, 7.0),
            ]
        )
        await db.commit()


async def test_top_usage_grouped_by_user():
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.api, START, END, "user", 10)
    # u1 (9 credits) ranks above u2 (2 credits); u1's cli key is another type.
    assert stats.total == 2
    assert len(stats.rows) == 2
    top = stats.rows[0]
    assert top.credits_spent == 9.0
    assert top.calls == 2
    assert top.api_key_label is None  # grouped by user: no key column
    assert top.account_created_at is not None

    # The other user is second with their single call.
    second = stats.rows[1]
    assert second.credits_spent == 2.0
    assert second.calls == 1


async def test_top_usage_grouped_by_api_key():
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.api, START, END, "api_key", 10)
    assert stats.total == 2
    top = stats.rows[0]
    # Masked key (4+4 window, same as ApiKey.masked_key): never the full 64-char key.
    assert top.api_key_label is not None
    assert "..." in top.api_key_label
    assert len(top.api_key_label) < 20
    assert top.api_key_created_at is not None


async def test_top_usage_chat_ranks_by_calls():
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.chat, START, END, "user", 10)
    assert stats.total == 1
    assert stats.rows[0].calls == 2
    assert stats.rows[0].credits_spent == 0.0


async def test_top_usage_excludes_suspended_accounts():
    """A suspended account must be excluded in BOTH grouping modes (by-user and by-key).

    Idempotent: guarded like _seed() — its 100-credit call would otherwise
    double on re-runs and shift the other tests' totals.
    """
    async with AsyncSessionLocal() as db:
        already = (await db.execute(select(ApiKey).where(ApiKey.name == f"{SUSPENDED}-api"))).scalars().first()
        if already is not None:
            return

        user = await get_or_create_user_by_wallet(db, SUSPENDED)
        await db.flush()
        user.suspended_at = DAY1
        api_key = ApiKey(key=ApiKey.generate_key(), name=f"{SUSPENDED}-api", user_id=user.id, type=ApiKeyType.api)
        db.add(api_key)
        await db.flush()
        db.add(_inference_call(api_key.id, DAY1, 100.0))  # would rank #1 if not suspended
        await db.commit()

    by_user = await StatsService.get_top_usage(ApiKeyType.api, START, END, "user", 10)
    # Positive assertions: the suspended account's 100-credit call must be absent,
    # and the leaderboard must be exactly the seeded pair (u1=9, u2=2).
    assert by_user.total == 2
    assert all(row.credits_spent != 100.0 for row in by_user.rows)
    assert by_user.rows[0].credits_spent == 9.0

    by_key = await StatsService.get_top_usage(ApiKeyType.api, START, END, "api_key", 10)
    assert by_key.total == 2
    assert all(row.credits_spent != 100.0 for row in by_key.rows)
    assert all(row.user_label != "unknown" for row in by_key.rows)


async def test_top_usage_liberclaw_identity():
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.liberclaw, START, END, "user", 10)
    assert stats.total == 1
    assert stats.rows[0].user_label == LC_TAG
    assert stats.rows[0].credits_spent == 7.0


async def test_top_usage_x402_is_empty():
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.x402, START, END, "user", 10)
    assert stats.rows == []
    assert stats.total == 0


async def test_active_users_paginated():
    await _seed()

    page1 = await StatsService.get_active_users(START, END, limit=1, offset=0)
    assert page1.total == 2  # u1 (10 credits across api+cli) + u2 (2 credits); liberclaw has no account
    assert len(page1.users) == 1
    top = page1.users[0]
    assert top.credits_spent == 10.0  # 9 api + 1 cli
    # u1's chat requests carry no credits, but count toward calls: 3 inference + 2 chat
    assert top.calls == 5
    assert top.account_created_at is not None
    assert top.first_active_at is not None
    assert top.last_active_at is not None

    page2 = await StatsService.get_active_users(START, END, limit=1, offset=1)
    assert len(page2.users) == 1
    assert page2.users[0].credits_spent == 2.0
    assert page2.users[0].calls == 1

    empty = await StatsService.get_active_users(START, END, limit=1, offset=2)
    assert empty.users == []


async def test_active_users_respect_date_range():
    await _seed()

    # A range before any activity sees nobody.
    stats = await StatsService.get_active_users(date(2019, 1, 1), date(2019, 1, 31), limit=20, offset=0)
    assert stats.total == 0
    assert stats.users == []
