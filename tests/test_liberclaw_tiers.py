"""Liberclaw tier names: the plan names LiberClaw itself uses.

Services run against the committed DB (they open their own sessions), so each
test cleans up its own rows.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.base import AsyncSessionLocal
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.liberclaw_user import LiberclawUser
from src.services.liberclaw import LiberclawService

pytestmark = pytest.mark.asyncio


async def _setup(tier="free") -> LiberclawUser:
    async with AsyncSessionLocal() as db:
        lc = LiberclawUser(user_id=uuid.uuid4().hex, user_type="email", tier=tier)
        db.add(lc)
        await db.commit()
        return lc


async def _cleanup(lc_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(LiberclawCreditGrant).where(LiberclawCreditGrant.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawUser).where(LiberclawUser.id == lc_id))
        await db.commit()


async def _stored_tier(lc_id) -> str:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(LiberclawUser.tier).where(LiberclawUser.id == lc_id))).scalar()


@pytest.mark.parametrize("tier", ["starter", "pro", "team"])
async def test_update_tier_stores_the_tier(tier):
    lc = await _setup()
    try:
        await LiberclawService.update_tier(lc.user_id, lc.user_type, tier)
        assert await _stored_tier(lc.id) == tier
    finally:
        await _cleanup(lc.id)


@pytest.mark.parametrize("tier", ["enterprise", "premium", "ultra"])
async def test_update_tier_rejects_an_unknown_name(tier):
    lc = await _setup()
    try:
        with pytest.raises(ValueError):
            await LiberclawService.update_tier(lc.user_id, lc.user_type, tier)
    finally:
        await _cleanup(lc.id)


async def test_grant_uses_the_from_tier_cap():
    lc = await _setup()
    try:
        amount = await LiberclawService.grant_extra_credits(
            user_id=lc.user_id,
            user_type=lc.user_type,
            from_tier="team",
            unused_fraction=0.5,
            external_reference=f"test:{uuid.uuid4().hex}",
        )
        assert amount == LIBERCLAW_TIERS["team"]["credits_limit"] * 0.5
    finally:
        await _cleanup(lc.id)


async def test_usage_falls_back_to_free_for_an_unknown_stored_tier():
    lc = await _setup(tier="premium")
    try:
        user = await LiberclawService.get_user(lc.user_id, lc.user_type)
        assert user.credits_limit == LIBERCLAW_TIERS["free"]["credits_limit"]
    finally:
        await _cleanup(lc.id)
