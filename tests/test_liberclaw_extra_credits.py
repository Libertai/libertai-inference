"""Liberclaw extra credits: upgrade-remainder grants extend the rolling-window cap.

Grants are created via LiberclawService.grant_extra_credits (idempotent on
external_reference), extend the gateway whitelist gate, and are consumed by
register_inference_call when a call overflows the tier cap — with the grant-paid
portion recorded on the row so window sums stay net of it.

These exercise services against the committed DB (they open their own sessions),
so each test cleans up its own rows.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from src.interfaces.api_keys import ApiKeyType
from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.liberclaw_user import LiberclawUser
from src.services.api_key import ApiKeyService
from src.services.liberclaw import LiberclawService

pytestmark = pytest.mark.asyncio

FREE_LIMIT = LIBERCLAW_TIERS["free"]["credits_limit"]


async def _setup(*, tier="free", usage=None, used_days_ago=1):
    """Liberclaw user + key with optional usage. Returns (lc_user, key_str)."""
    now = datetime.now()
    async with AsyncSessionLocal() as db:
        lc = LiberclawUser(user_id=uuid.uuid4().hex, user_type="email", tier=tier)
        db.add(lc)
        await db.flush()
        key = ApiKeyDB(
            key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, type=ApiKeyType.liberclaw, liberclaw_user_id=lc.id
        )
        db.add(key)
        await db.flush()
        if usage:
            call = InferenceCall(api_key_id=key.id, credits_used=usage, model_name="m")
            call.used_at = now - timedelta(days=used_days_ago)
            db.add(call)
        await db.commit()
        return lc, key.key


async def _cleanup(lc_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawCreditGrant).where(LiberclawCreditGrant.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawUser).where(LiberclawUser.id == lc_id))
        await db.commit()


async def _grant(lc, fraction=0.5, from_tier="free", ref=None):
    return await LiberclawService.grant_extra_credits(
        user_id=lc.user_id,
        user_type=lc.user_type,
        from_tier=from_tier,
        unused_fraction=fraction,
        external_reference=ref or f"test:{uuid.uuid4().hex}",
    )


async def _valid_keys() -> list[str]:
    return (await ApiKeyService.get_admin_all_api_keys()).valid


# --------------------------------------------------------------------- granting


async def test_grant_amount_is_fraction_of_tier_cap():
    lc, _ = await _setup()
    try:
        amount = await _grant(lc, fraction=0.5, from_tier="pro")
        assert amount == LIBERCLAW_TIERS["pro"]["credits_limit"] * 0.5
    finally:
        await _cleanup(lc.id)


async def test_grant_idempotent_on_external_reference():
    lc, _ = await _setup()
    try:
        ref = f"upgrade_remainder:{uuid.uuid4()}"
        first = await _grant(lc, fraction=0.4, ref=ref)
        second = await _grant(lc, fraction=0.9, ref=ref)  # retry with different args
        assert second == first
        async with AsyncSessionLocal() as db:
            grants = (
                (await db.execute(select(LiberclawCreditGrant).where(LiberclawCreditGrant.liberclaw_user_id == lc.id)))
                .scalars()
                .all()
            )
        assert len(grants) == 1
        assert grants[0].amount == first
    finally:
        await _cleanup(lc.id)


async def test_grant_rejects_bad_inputs():
    lc, _ = await _setup()
    try:
        with pytest.raises(ValueError):
            await _grant(lc, from_tier="nope")
        with pytest.raises(ValueError):
            await _grant(lc, fraction=0.0)
        with pytest.raises(ValueError):
            await _grant(lc, fraction=1.5)
        with pytest.raises(ValueError):
            await LiberclawService.grant_extra_credits(
                user_id="ghost", user_type="email", from_tier="free", unused_fraction=0.5, external_reference="x"
            )
    finally:
        await _cleanup(lc.id)


# --------------------------------------------------------------------- gateway gate


async def test_over_cap_key_stays_valid_with_grant():
    lc, key = await _setup(usage=FREE_LIMIT + 1)
    try:
        assert key not in await _valid_keys()
        await _grant(lc, fraction=0.5)  # +10 credits headroom
        assert key in await _valid_keys()
    finally:
        await _cleanup(lc.id)


async def test_key_invalid_once_grant_exhausted():
    lc, key = await _setup(usage=FREE_LIMIT + 1)
    try:
        await _grant(lc, fraction=0.5)
        async with AsyncSessionLocal() as db:
            consumed = await LiberclawService.consume_extra_credits(db, lc.id, FREE_LIMIT * 0.5)
            await db.commit()
        assert consumed == FREE_LIMIT * 0.5
        assert key not in await _valid_keys()
    finally:
        await _cleanup(lc.id)


# --------------------------------------------------------------------- billing split


async def test_overflow_consumes_grant_and_marks_row():
    lc, key = await _setup(usage=FREE_LIMIT)  # cap exactly exhausted
    try:
        granted = await _grant(lc, fraction=0.5)
        assert await ApiKeyService.register_inference_call(key=key, credits_used=3.0, model_name="m")
        async with AsyncSessionLocal() as db:
            left = await LiberclawService.extra_credits_left(db, lc.id)
            row = (
                (
                    await db.execute(
                        select(InferenceCall)
                        .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
                        .where(ApiKeyDB.key == key, InferenceCall.credits_used == 3.0)
                    )
                )
                .scalars()
                .one()
            )
        assert left == granted - 3.0
        assert row.liberclaw_extra_credits_used == 3.0
        # Net window usage unchanged by the grant-paid call.
        user = await LiberclawService.get_user(user_id=lc.user_id, user_type=lc.user_type)
        assert user.credits_used == FREE_LIMIT
        assert user.extra_credits_left == left
    finally:
        await _cleanup(lc.id)


async def test_call_straddling_cap_only_overflow_hits_grant():
    lc, key = await _setup(usage=FREE_LIMIT - 1)  # 1 credit of cap headroom left
    try:
        granted = await _grant(lc, fraction=0.5)
        assert await ApiKeyService.register_inference_call(key=key, credits_used=3.0, model_name="m")
        async with AsyncSessionLocal() as db:
            left = await LiberclawService.extra_credits_left(db, lc.id)
        assert left == granted - 2.0  # 1 covered by cap, 2 by the grant
    finally:
        await _cleanup(lc.id)


async def test_within_cap_call_leaves_grant_untouched():
    lc, key = await _setup(usage=1.0)
    try:
        granted = await _grant(lc, fraction=0.5)
        assert await ApiKeyService.register_inference_call(key=key, credits_used=2.0, model_name="m")
        async with AsyncSessionLocal() as db:
            left = await LiberclawService.extra_credits_left(db, lc.id)
            row = (
                (
                    await db.execute(
                        select(InferenceCall)
                        .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
                        .where(ApiKeyDB.key == key, InferenceCall.credits_used == 2.0)
                    )
                )
                .scalars()
                .one()
            )
        assert left == granted
        assert row.liberclaw_extra_credits_used is None
    finally:
        await _cleanup(lc.id)


async def test_overflow_without_grant_records_nothing():
    lc, key = await _setup(usage=FREE_LIMIT + 1)
    try:
        assert await ApiKeyService.register_inference_call(key=key, credits_used=2.0, model_name="m")
        async with AsyncSessionLocal() as db:
            row = (
                (
                    await db.execute(
                        select(InferenceCall)
                        .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
                        .where(ApiKeyDB.key == key, InferenceCall.credits_used == 2.0)
                    )
                )
                .scalars()
                .one()
            )
        assert row.liberclaw_extra_credits_used is None
    finally:
        await _cleanup(lc.id)


async def test_consume_partial_when_grants_short():
    lc, _ = await _setup()
    try:
        await _grant(lc, fraction=0.25)  # 5 credits on free tier
        async with AsyncSessionLocal() as db:
            consumed = await LiberclawService.consume_extra_credits(db, lc.id, 8.0)
            await db.commit()
            left = await LiberclawService.extra_credits_left(db, lc.id)
        assert consumed == 5.0
        assert left == 0.0
    finally:
        await _cleanup(lc.id)
