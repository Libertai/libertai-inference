"""Grandfathered liberclaw caps (``credits_limit_override``) through the 2026-09 repricing.

A subscriber live at deploy keeps the cap they bought while they stay on the same tier: every
cap read (gateway gate, usage report split, usage endpoint, upgrade remainders) goes through the
override, and any tier change or subscription end drops it.

Service-level tests run against the committed DB (the services open their own sessions), so they
clean up their own rows; the PaymentManager tests use the rolled-back ``db`` fixture.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from src.config import config
from src.interfaces.api_keys import ApiKeyType
from src.liberclaw_tiers import LIBERCLAW_TIERS, effective_credits_limit
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.services.api_key import ApiKeyService
from src.services.liberclaw import LiberclawService
from src.services.payments.base import PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager
from src.services.payments.owner import Owner
from tests.test_payment_manager import FakeProvider, _event_types
from tests.test_payment_manager_liberclaw import _lc_user, _lclw_sub

STARTER_LIMIT = LIBERCLAW_TIERS["starter"]["credits_limit"]
OLD_STARTER_LIMIT = 100.0


async def _setup(*, tier="starter", override=None, usage=None) -> tuple[LiberclawUser, str]:
    """Liberclaw user + key, optionally grandfathered and with usage a day old."""
    async with AsyncSessionLocal() as db:
        lc = LiberclawUser(user_id=uuid.uuid4().hex, user_type="email", tier=tier)
        lc.credits_limit_override = override
        db.add(lc)
        await db.flush()
        key = ApiKeyDB(
            key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, type=ApiKeyType.liberclaw, liberclaw_user_id=lc.id
        )
        db.add(key)
        await db.flush()
        if usage:
            call = InferenceCall(api_key_id=key.id, credits_used=usage, model_name="m")
            call.used_at = datetime.now() - timedelta(days=1)
            db.add(call)
        await db.commit()
        return lc, key.key


async def _cleanup(lc_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawCreditGrant).where(LiberclawCreditGrant.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawUser).where(LiberclawUser.id == lc_id))
        await db.commit()


async def _stored(lc_id) -> LiberclawUser:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(LiberclawUser).where(LiberclawUser.id == lc_id))).scalar_one()


async def _grant(lc, ref=None, from_tier="starter", fraction=0.5) -> float:
    return await LiberclawService.grant_extra_credits(
        user_id=lc.user_id,
        user_type=lc.user_type,
        from_tier=from_tier,
        unused_fraction=fraction,
        external_reference=ref or f"test:{uuid.uuid4().hex}",
    )


# --------------------------------------------------------------------- new caps


def test_repriced_caps():
    assert {t: c["credits_limit"] for t, c in LIBERCLAW_TIERS.items()} == {
        "free": 5.0,
        "starter": 30.0,
        "pro": 100.0,
        "team": 300.0,
    }
    assert {c["rolling_window_days"] for c in LIBERCLAW_TIERS.values()} == {30}


def test_effective_limit_prefers_the_override():
    lc = LiberclawUser(user_id="x", user_type="email", tier="pro")
    assert effective_credits_limit(lc) == LIBERCLAW_TIERS["pro"]["credits_limit"]
    lc.credits_limit_override = 500.0
    assert effective_credits_limit(lc) == 500.0
    # An explicit 0 is a real cap, not "unset".
    lc.credits_limit_override = 0.0
    assert effective_credits_limit(lc) == 0.0


def test_effective_limit_falls_back_to_free_for_an_unknown_tier():
    lc = LiberclawUser(user_id="x", user_type="email", tier="premium")
    assert effective_credits_limit(lc) == LIBERCLAW_TIERS["free"]["credits_limit"]


# --------------------------------------------------------------------- reads


@pytest.mark.asyncio
async def test_gate_uses_the_override():
    """Usage between the new starter cap and the grandfathered one: valid only with the override."""
    usage = (STARTER_LIMIT + OLD_STARTER_LIMIT) / 2
    kept, kept_key = await _setup(override=OLD_STARTER_LIMIT, usage=usage)
    cut, cut_key = await _setup(usage=usage)
    try:
        valid = (await ApiKeyService.get_admin_all_api_keys()).valid
        assert kept_key in valid
        assert cut_key not in valid
    finally:
        await _cleanup(kept.id)
        await _cleanup(cut.id)


@pytest.mark.asyncio
async def test_gate_blocks_at_the_override():
    lc, key = await _setup(override=OLD_STARTER_LIMIT, usage=OLD_STARTER_LIMIT)
    try:
        assert key not in (await ApiKeyService.get_admin_all_api_keys()).valid
    finally:
        await _cleanup(lc.id)


@pytest.mark.asyncio
async def test_usage_report_splits_against_the_override():
    """Past the new cap but within the grandfathered one, a call is cap-covered: the grant is untouched."""
    lc, key = await _setup(override=OLD_STARTER_LIMIT, usage=STARTER_LIMIT + 10)
    try:
        granted = await _grant(lc)
        assert await ApiKeyService.register_inference_call(key=key, credits_used=3.0, model_name="m")
        async with AsyncSessionLocal() as db:
            assert await LiberclawService.extra_credits_left(db, lc.id) == granted
    finally:
        await _cleanup(lc.id)


@pytest.mark.asyncio
async def test_usage_report_without_override_overflows_into_the_grant():
    lc, key = await _setup(usage=STARTER_LIMIT + 10)
    try:
        granted = await _grant(lc)
        assert await ApiKeyService.register_inference_call(key=key, credits_used=3.0, model_name="m")
        async with AsyncSessionLocal() as db:
            assert await LiberclawService.extra_credits_left(db, lc.id) == granted - 3.0
    finally:
        await _cleanup(lc.id)


@pytest.mark.asyncio
async def test_usage_endpoint_reports_the_override():
    lc, _ = await _setup(override=OLD_STARTER_LIMIT)
    try:
        user = await LiberclawService.get_user(lc.user_id, lc.user_type)
        assert user.credits_limit == OLD_STARTER_LIMIT
    finally:
        await _cleanup(lc.id)


@pytest.mark.asyncio
async def test_remainder_grant_prorates_the_override_on_the_current_tier():
    lc, _ = await _setup(override=OLD_STARTER_LIMIT)
    try:
        assert await _grant(lc, from_tier="starter", fraction=0.5) == OLD_STARTER_LIMIT * 0.5
    finally:
        await _cleanup(lc.id)


@pytest.mark.asyncio
async def test_remainder_grant_for_another_tier_ignores_the_override():
    """The override belongs to the current tier; a remainder of any other tier is that tier's cap."""
    lc, _ = await _setup(tier="pro", override=500.0)
    try:
        assert await _grant(lc, from_tier="starter", fraction=0.5) == STARTER_LIMIT * 0.5
    finally:
        await _cleanup(lc.id)


# --------------------------------------------------------------------- clearing


def test_set_tier_keeps_the_override_on_the_same_tier():
    lc = LiberclawUser(user_id="x", user_type="email", tier="pro")
    lc.credits_limit_override = 500.0
    LiberclawService.set_tier(lc, "pro")
    assert lc.credits_limit_override == 500.0


@pytest.mark.parametrize("new_tier", ["free", "starter", "team"])
def test_set_tier_drops_the_override_on_a_change(new_tier):
    lc = LiberclawUser(user_id="x", user_type="email", tier="pro")
    lc.credits_limit_override = 500.0
    LiberclawService.set_tier(lc, new_tier)
    assert lc.tier == new_tier
    assert lc.credits_limit_override is None


def test_set_tier_drops_the_override_on_free_even_when_already_free():
    lc = LiberclawUser(user_id="x", user_type="email", tier="free")
    lc.credits_limit_override = 10.0
    LiberclawService.set_tier(lc, "free")
    assert lc.credits_limit_override is None


@pytest.mark.asyncio
async def test_update_tier_route_path_keeps_on_same_tier_and_clears_on_change():
    lc, _ = await _setup(override=OLD_STARTER_LIMIT)
    try:
        await LiberclawService.update_tier(lc.user_id, lc.user_type, "starter")
        assert (await _stored(lc.id)).credits_limit_override == OLD_STARTER_LIMIT
        await LiberclawService.update_tier(lc.user_id, lc.user_type, "pro")
        stored = await _stored(lc.id)
        assert stored.tier == "pro"
        assert stored.credits_limit_override is None
    finally:
        await _cleanup(lc.id)


# --------------------------------------------------------------------- webhook / expiry paths


def _completed(n: int, psub: str = "psub_1") -> PaymentEvent:
    return PaymentEvent(
        provider="fake",
        type=PaymentEventType.order_completed,
        provider_event_id=f"ORDER_COMPLETED:gf_{n}",
        provider_subscription_id=psub,
        order_id=f"gf_order_{n}",
    )


async def _activated_starter(db, monkeypatch) -> tuple[PaymentManager, Owner, LiberclawUser, PlanSubscription]:
    """A paid, active LCLW starter subscription whose owner is grandfathered at the old cap."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    account_id = uuid.uuid4()
    owner = Owner.for_liberclaw(account_id, email=f"{account_id.hex}@example.com")
    lc_user = await _lc_user(db, account_id, tier="free")
    mgr = PaymentManager(FakeProvider(), db)
    await mgr.start_checkout(owner, tier="starter", redirect_url="http://x", currency="EUR")
    await mgr.handle_event(_completed(1))
    sub = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.liberclaw_account_id == account_id, PlanSubscription.status == "active"
            )
        )
    ).scalar_one()
    lc_user.credits_limit_override = OLD_STARTER_LIMIT
    await db.flush()
    return mgr, owner, lc_user, sub


@pytest.mark.asyncio
async def test_renewal_on_the_same_tier_keeps_the_override(db, monkeypatch):
    mgr, _, lc_user, sub = await _activated_starter(db, monkeypatch)
    await mgr.handle_event(_completed(2))
    assert "renewed" in await _event_types(db, sub.id)
    await db.refresh(lc_user)
    assert lc_user.tier == "starter"
    assert lc_user.credits_limit_override == OLD_STARTER_LIMIT


@pytest.mark.asyncio
async def test_upgrade_prorates_the_override_then_drops_it(db, monkeypatch):
    mgr, owner, lc_user, old_sub = await _activated_starter(db, monkeypatch)
    await mgr.upgrade(owner, new_tier="pro", redirect_url="http://x", currency="EUR")
    await mgr.handle_event(_completed(2, psub="psub_2"))

    grant = (
        await db.execute(
            select(LiberclawCreditGrant).where(
                LiberclawCreditGrant.external_reference == f"upgrade_remainder:{old_sub.id}"
            )
        )
    ).scalar_one()
    # FakeProvider cycles are 10 days in, 20 left.
    assert grant.amount == pytest.approx(OLD_STARTER_LIMIT * (20 / 30), abs=0.05)
    await db.refresh(lc_user)
    assert lc_user.tier == "pro"
    assert lc_user.credits_limit_override is None


@pytest.mark.asyncio
async def test_terminal_cancel_drops_the_override(db, monkeypatch):
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    account_id = uuid.uuid4()
    lc_user = await _lc_user(db, account_id, tier="starter")
    lc_user.credits_limit_override = OLD_STARTER_LIMIT
    sub = _lclw_sub(account_id, status="active", current_period_end=datetime.now() - timedelta(hours=1))
    db.add(sub)
    await db.flush()

    await PaymentManager(FakeProvider(), db).handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.subscription_cancelled,
            provider_event_id="SUBSCRIPTION_CANCELLED:gf",
            provider_subscription_id="psub_lclw",
        )
    )

    await db.refresh(lc_user)
    assert lc_user.tier == "free"
    assert lc_user.credits_limit_override is None


@pytest.mark.asyncio
async def test_expiry_drops_the_override(db, monkeypatch):
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    account_id = uuid.uuid4()
    lc_user = await _lc_user(db, account_id, tier="starter")
    lc_user.credits_limit_override = OLD_STARTER_LIMIT
    sub = _lclw_sub(
        account_id,
        status="active",
        cancel_at_period_end=True,
        provider_cancelled=True,
        current_period_end=datetime.now() - timedelta(hours=1),
    )
    db.add(sub)
    await db.flush()

    await PaymentManager(FakeProvider(), db).check_expirations()

    assert (await db.get(PlanSubscription, sub.id)).status == "expired"
    await db.refresh(lc_user)
    assert lc_user.tier == "free"
    assert lc_user.credits_limit_override is None
