"""Suspended liberclaw identities are excluded from global statistics.

Two liberclaw identities get identical traffic; one is suspended through the
suspended-accounts sync, so any statistic still counting it shows a doubled figure.
Rows are stamped in a fixed 2021 window no other test uses.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import select

from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.liberclaw_user import LiberclawUser
from src.services.liberclaw import LiberclawService
from src.services.stats import StatsService

START = date(2021, 4, 1)
END = date(2021, 4, 2)
DAY1 = datetime(2021, 4, 1, 12, 0, 0)

LIVE_ACCOUNT = uuid.UUID("00000000-0000-4000-8000-00000000a11e")
BANNED_ACCOUNT = uuid.UUID("00000000-0000-4000-8000-00000000ba00")
BANNED = (BANNED_ACCOUNT, "lc-susp-ban@test.local", "email")
# Row created without liberclaw_account_id: only its email identity can match it.
NO_ID_EMAIL = "lc-susp-noid@test.local"

CREDITS = 0.5


async def _seed() -> None:
    """Idempotent: the assertions are absolute counts over a shared window."""
    async with AsyncSessionLocal() as db:
        already = (await db.execute(select(ApiKey).where(ApiKey.name == "lc-susp-live"))).scalars().first()
        if already is None:
            live = LiberclawUser(
                user_id="lc-susp-live@test.local", user_type="email", liberclaw_account_id=LIVE_ACCOUNT
            )
            banned = LiberclawUser(
                user_id="lc-susp-ban@test.local", user_type="email", liberclaw_account_id=BANNED_ACCOUNT
            )
            no_id = LiberclawUser(user_id=NO_ID_EMAIL, user_type="email")
            db.add_all([live, banned, no_id])
            await db.flush()
            keys = [
                ApiKey(key=ApiKey.generate_key(), name=name, liberclaw_user_id=lc.id, type=ApiKeyType.liberclaw)
                for name, lc in (("lc-susp-live", live), ("lc-susp-ban", banned))
            ]
            db.add_all(keys)
            await db.flush()
            for key in keys:
                call = InferenceCall(
                    api_key_id=key.id,
                    credits_used=CREDITS,
                    model_name="lc-suspended-test-model",
                    input_tokens=100,
                    output_tokens=20,
                    tier_credits_used=CREDITS,
                )
                call.used_at = DAY1
                db.add(call)
            await db.commit()
    await LiberclawService.sync_suspended_accounts([BANNED])


async def _suspended_at(email: str):
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(LiberclawUser.suspended_at).where(
                    LiberclawUser.user_id == email, LiberclawUser.user_type == "email"
                )
            )
        ).scalar_one()


async def test_liberclaw_stats_exclude_suspended_identity():
    await _seed()

    calls = await StatsService._get_inference_api_stats(ApiKeyType.liberclaw, START, END)
    assert calls.total_calls == 1

    credits = await StatsService._get_inference_credits_stats(ApiKeyType.liberclaw, START, END)
    assert credits.total_credits_used == CREDITS

    users = await StatsService._get_inference_users_stats(ApiKeyType.liberclaw, START, END)
    assert users.total_unique_users == 1

    agg = await StatsService.get_global_users_stats(START, END)
    assert agg.total_unique_users == 1

    for group_by in ("user", "api_key"):
        top = await StatsService.get_top_usage(ApiKeyType.liberclaw, START, END, group_by, 10)
        assert top.total == 1
        assert len(top.rows) == 1


async def test_sync_sets_and_clears_suspension():
    await _seed()
    first = await _suspended_at(BANNED[1])
    assert first is not None
    assert await _suspended_at("lc-susp-live@test.local") is None

    # Re-sending the same set keeps the original timestamp.
    await LiberclawService.sync_suspended_accounts([BANNED])
    assert await _suspended_at(BANNED[1]) == first

    # A row with no liberclaw_account_id matches on its identity.
    await LiberclawService.sync_suspended_accounts([BANNED, (uuid.uuid4(), NO_ID_EMAIL, "email")])
    assert await _suspended_at(NO_ID_EMAIL) is not None

    # An account missing from the set is live again.
    await LiberclawService.sync_suspended_accounts([])
    assert await _suspended_at(BANNED[1]) is None
    assert await _suspended_at(NO_ID_EMAIL) is None

    await LiberclawService.sync_suspended_accounts([BANNED])
