"""Top-users-by-usage and paginated active-users stats.

Seeds rows through the real ``AsyncSessionLocal`` (committed) so ``StatsService`` — which
opens its own session — can see them, mirroring the other stats tests. Everything is
stamped in a fixed 2020 window and queried over exactly that range, so committed rows
from other tests (stamped "now") never fall in range and can't pollute the counts.
"""

from datetime import date, datetime

import pytest
from sqlalchemy import select

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.inference_call import InferenceCall
from src.models.liberclaw_user import LiberclawUser
from src.services.aleph import aleph_service
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


UNKNOWN_MODEL_USER = "0xDA0100000000000000000000000000000000B004"
UNKNOWN_MODEL_DAY = datetime(2020, 2, 10, 12, 0, 0)
UNKNOWN_MODEL_WINDOW = date(2020, 2, 10)


async def _fake_price(
    model_id: str, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0, image_count: int = 0
) -> float:
    """Half a credit per token, so a seeded chat request is worth 1.0."""
    if model_id != "test-model":
        raise ValueError(f"Invalid model ID: {model_id}")
    return (input_tokens + output_tokens) * 0.5


@pytest.fixture(autouse=True)
def _priced_chat(monkeypatch):
    """Chat spend comes from the Aleph price list; keep the suite off the network."""
    monkeypatch.setattr(aleph_service, "calculate_price", _fake_price)


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
        # Second api key for u1 so the by-key view exercises duplicate emails
        # (the PR's headline behavior: one row per key, same user twice).
        u1_api2 = ApiKey(key=ApiKey.generate_key(), name=f"{API_KEY_TAG}-1b", user_id=user1.id, type=ApiKeyType.api)
        u1_cli = ApiKey(key=ApiKey.generate_key(), name=f"{CLI_KEY_TAG}-1", user_id=user1.id, type=ApiKeyType.cli)
        u1_chat = ApiKey(key=ApiKey.generate_key(), name=f"{CHAT_KEY_TAG}-1", user_id=user1.id, type=ApiKeyType.chat)
        u2_api = ApiKey(key=ApiKey.generate_key(), name=f"{API_KEY_TAG}-2", user_id=user2.id, type=ApiKeyType.api)
        lc_key = ApiKey(
            key=ApiKey.generate_key(),
            name=LC_TAG,
            liberclaw_user_id=liberclaw_user.id,
            type=ApiKeyType.liberclaw,
        )
        db.add_all([u1_api, u1_api2, u1_cli, u1_chat, u2_api, lc_key])
        await db.flush()

        db.add_all(
            [
                # u1: 3 api calls (9 credits) + 1 cli call (1 credit) + 2 chat requests
                _inference_call(u1_api.id, DAY1, 4.0),
                _inference_call(u1_api.id, DAY2, 5.0),
                _inference_call(u1_api2.id, DAY1, 0.5),
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
    # u1 (9.5 credits over 2 keys) ranks above u2 (2 credits); cli/chat are other types.
    assert stats.total == 2
    assert len(stats.rows) == 2
    top = stats.rows[0]
    assert top.credits_spent == 9.5
    assert top.calls == 3
    assert top.api_key_label is None  # grouped by user: no key column
    assert top.account_created_at is not None

    # The other user is second with their single call.
    second = stats.rows[1]
    assert second.credits_spent == 2.0
    assert second.calls == 1


async def test_top_usage_grouped_by_api_key():
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.api, START, END, "api_key", 10)
    # 3 keys seeded: u1's two api keys (duplicate email) + u2's one.
    # Ranking by credits DESC: u1_api (9.0), u2_api (2.0), u1_api2 (0.5) —
    # u1's keys sit at ranks 1 and 3, so the duplicate labels are not adjacent.
    assert stats.total == 3
    labels = [row.user_label for row in stats.rows]
    assert labels[0] == labels[2]  # u1's two keys: same email twice (rank 1 and 3)
    assert labels[1] != labels[0]  # u2's key sits between them
    top = stats.rows[0]
    # Masked key (4+4 window, same as ApiKey.masked_key): never the full 64-char key.
    assert top.api_key_label is not None
    assert "..." in top.api_key_label
    assert len(top.api_key_label) < 20
    assert top.api_key_created_at is not None


async def test_top_usage_chat_priced_from_tokens():
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.chat, START, END, "user", 10)
    assert stats.total == 1
    assert stats.rows[0].calls == 2
    # 2 requests x (1 input + 1 output) token at half a credit each
    assert stats.rows[0].credits_spent == 2.0


async def test_top_usage_chat_prices_unknown_model_as_zero():
    """A model the price list no longer carries must not fail the leaderboard."""
    async with AsyncSessionLocal() as db:
        already = (
            (await db.execute(select(ApiKey).where(ApiKey.name == f"{UNKNOWN_MODEL_USER}-chat"))).scalars().first()
        )
        if already is None:
            user = await get_or_create_user_by_wallet(db, UNKNOWN_MODEL_USER)
            await db.flush()
            key = ApiKey(
                key=ApiKey.generate_key(), name=f"{UNKNOWN_MODEL_USER}-chat", user_id=user.id, type=ApiKeyType.chat
            )
            db.add(key)
            await db.flush()
            request = ChatRequest(
                api_key_id=key.id, input_tokens=10, output_tokens=10, cached_tokens=0, model_name="retired-model"
            )
            request.created_at = UNKNOWN_MODEL_DAY
            db.add(request)
            await db.commit()

    stats = await StatsService.get_top_usage(ApiKeyType.chat, UNKNOWN_MODEL_WINDOW, UNKNOWN_MODEL_WINDOW, "user", 10)
    assert stats.total == 1
    assert stats.rows[0].calls == 1
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
    # and the leaderboard must be exactly the seeded pair (u1=9.5, u2=2).
    assert by_user.total == 2
    assert all(row.credits_spent != 100.0 for row in by_user.rows)
    assert by_user.rows[0].credits_spent == 9.5

    by_key = await StatsService.get_top_usage(ApiKeyType.api, START, END, "api_key", 10)
    # Same 3 keys as test_top_usage_grouped_by_api_key; total must match rows
    # (the total-count query applies the same suspension rule as the rows query).
    assert by_key.total == 3
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
    assert page1.total == 2  # u1 (12.5 credits across api+cli+chat) + u2 (2 credits); liberclaw has no account
    assert len(page1.users) == 1
    top = page1.users[0]
    assert top.credits_spent == 12.5  # 9.5 api + 1 cli + 2 chat
    # 4 inference calls + 2 chat requests
    assert top.calls == 6
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


async def test_top_usage_limit_truncates_rows_not_total():
    """limit truncates the leaderboard but not the footer's "N of M" total.

    The frontend footer shows ``rows/limit of total``, so total must stay the full
    count even when fewer rows are returned.
    """
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.api, START, END, "user", limit=1)
    assert len(stats.rows) == 1
    assert stats.total == 2  # unchanged by the limit, exactly what "1 of 2" needs
    assert stats.rows[0].credits_spent == 9.5  # still the top user

    by_key = await StatsService.get_top_usage(ApiKeyType.api, START, END, "api_key", limit=2)
    assert len(by_key.rows) == 2
    assert by_key.total == 3  # the 3 seeded keys, regardless of the limit


async def test_top_usage_grouped_by_api_key_chat_priced_from_tokens():
    """By-key grouping must also work for chat, whose spend is priced from the tokens."""
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.chat, START, END, "api_key", 10)
    assert stats.total == 1
    row = stats.rows[0]
    assert row.calls == 2
    assert row.credits_spent == 2.0
    assert row.api_key_label is not None
    # fallback/copies are the by-key labels of a real account user (u1's chat key)
    assert row.user_label != "unknown"


async def test_top_usage_grouped_by_api_key_liberclaw():
    """By-key grouping for a liberclaw key uses the liberclaw identity as the label."""
    await _seed()

    stats = await StatsService.get_top_usage(ApiKeyType.liberclaw, START, END, "api_key", 10)
    assert stats.total == 1
    row = stats.rows[0]
    assert row.user_label == LC_TAG  # liberclaw user_id doubles as the label
    assert row.credits_spent == 7.0
    assert row.api_key_label is not None
