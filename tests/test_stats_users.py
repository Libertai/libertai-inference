"""Distinct-user (DAU) stats per key type + deduplicated aggregate.

Seeds rows through the real ``AsyncSessionLocal`` (committed) so ``StatsService`` — which
opens its own session — can see them, mirroring the billing tests. Everything is stamped
in a fixed 2020 window and queried over exactly that range, so committed rows from other
tests (stamped "now") never fall in range and can't pollute the counts.
"""

from datetime import date, datetime

from sqlalchemy import select

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.inference_call import InferenceCall
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.services.stats import StatsService
from src.services.users import get_or_create_user_by_wallet

START = date(2020, 1, 1)
END = date(2020, 1, 3)
DAY1 = datetime(2020, 1, 1, 12, 0, 0)
DAY2 = datetime(2020, 1, 2, 12, 0, 0)

U1 = "0xDA0100000000000000000000000000000000A001"
U2 = "0xDA0100000000000000000000000000000000A002"


def _inference_call(api_key_id, when: datetime) -> InferenceCall:
    call = InferenceCall(api_key_id=api_key_id, credits_used=0.0, model_name="test-model")
    call.used_at = when
    return call


def _chat_request(api_key_id, when: datetime) -> ChatRequest:
    cr = ChatRequest(api_key_id=api_key_id, input_tokens=1, output_tokens=1, cached_tokens=0, model_name="test-model")
    cr.created_at = when
    return cr


async def _seed() -> None:
    async with AsyncSessionLocal() as db:
        user1 = await get_or_create_user_by_wallet(db, U1)
        user2 = await get_or_create_user_by_wallet(db, U2)
        await db.flush()

        liberclaw_user = (
            await db.execute(
                select(LiberclawUser).where(
                    LiberclawUser.user_id == "dau-lc-user", LiberclawUser.user_type == "telegram"
                )
            )
        ).scalar_one_or_none()
        if liberclaw_user is None:
            liberclaw_user = LiberclawUser(user_id="dau-lc-user", user_type="telegram")
            db.add(liberclaw_user)
            await db.flush()

        u1_api = ApiKey(key=ApiKey.generate_key(), name="dau-u1-api", user_id=user1.id, type=ApiKeyType.api)
        u2_api = ApiKey(key=ApiKey.generate_key(), name="dau-u2-api", user_id=user2.id, type=ApiKeyType.api)
        u1_cli = ApiKey(key=ApiKey.generate_key(), name="dau-u1-cli", user_id=user1.id, type=ApiKeyType.cli)
        u1_chat = ApiKey(key=ApiKey.generate_key(), name="dau-u1-chat", user_id=user1.id, type=ApiKeyType.chat)
        lc_key = ApiKey(
            key=ApiKey.generate_key(),
            name="dau-lc",
            liberclaw_user_id=liberclaw_user.id,
            type=ApiKeyType.liberclaw,
        )
        db.add_all([u1_api, u2_api, u1_cli, u1_chat, lc_key])
        await db.flush()

        existing_sub = (
            await db.execute(select(PlanSubscription).where(PlanSubscription.user_id == user1.id))
        ).scalar_one_or_none()
        if existing_sub is None:
            db.add(PlanSubscription(user_id=user1.id, tier="plus", provider="revolut", status="active"))

        db.add_all(
            [
                # Day 1: api users u1 + u2, cli user u1, liberclaw user, chat user u1
                _inference_call(u1_api.id, DAY1),
                _inference_call(u2_api.id, DAY1),
                _inference_call(u1_cli.id, DAY1),
                _inference_call(lc_key.id, DAY1),
                _chat_request(u1_chat.id, DAY1),
                # Day 2: only api user u1
                _inference_call(u1_api.id, DAY2),
            ]
        )
        await db.commit()


async def test_dau_per_section_and_aggregate():
    await _seed()

    # --- API: u1 + u2 on day 1, u1 on day 2; 2 unique over range ---
    api = await StatsService._get_inference_users_stats(ApiKeyType.api, START, END)
    assert api.total_unique_users == 2
    api_by_day = {d.date: d.active_users for d in api.daily_active_users}
    assert api_by_day == {"2020-01-01": 2, "2020-01-02": 1}

    # --- CLI: only u1 on day 1 ---
    cli = await StatsService._get_inference_users_stats(ApiKeyType.cli, START, END)
    assert cli.total_unique_users == 1
    assert {d.date: d.active_users for d in cli.daily_active_users} == {"2020-01-01": 1}

    # --- Liberclaw: counted via liberclaw_user_id ---
    liberclaw = await StatsService._get_inference_users_stats(ApiKeyType.liberclaw, START, END)
    assert liberclaw.total_unique_users == 1
    assert {d.date: d.active_users for d in liberclaw.daily_active_users} == {"2020-01-01": 1}

    # --- Chat: u1 on day 1 (separate chat_requests table) ---
    chat = await StatsService.get_global_chat_users_stats(START, END)
    assert chat.total_unique_users == 1
    assert {d.date: d.active_users for d in chat.daily_active_users} == {"2020-01-01": 1}

    # --- Aggregate: u1 shared across api/cli/chat counts once; +u2 +liberclaw ---
    agg = await StatsService.get_global_users_stats(START, END)
    assert agg.total_unique_users == 3  # u1, u2, liberclaw user
    agg_by_day = {d.date: d.active_users for d in agg.daily_active_users}
    assert agg_by_day == {"2020-01-01": 3, "2020-01-02": 1}

    # --- x402: keys carry no user identity, so distinct-user counts are zero (NULLs excluded) ---
    x402 = await StatsService._get_inference_users_stats(ApiKeyType.x402, START, END)
    assert x402.total_unique_users == 0
    assert x402.daily_active_users == []


async def test_aggregate_users_by_tier():
    await _seed()

    agg = await StatsService.get_global_users_stats(START, END)
    by_tier = {(d.date, d.tier): d.active_users for d in agg.daily_active_users_by_tier}
    # Day 1: u1 (active plus sub), u2 (no sub -> free), liberclaw user. Day 2: u1 only.
    assert by_tier == {
        ("2020-01-01", "free"): 1,
        ("2020-01-01", "liberclaw"): 1,
        ("2020-01-01", "plus"): 1,
        ("2020-01-02", "plus"): 1,
    }
    # Per-tier counts sum to the combined series day by day.
    combined = {d.date: d.active_users for d in agg.daily_active_users}
    for day, total in combined.items():
        assert sum(n for (d, _t), n in by_tier.items() if d == day) == total
