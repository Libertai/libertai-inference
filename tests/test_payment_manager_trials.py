"""Trials, manual overrides, the trial->paid supersede branch, product expiry rules and the
LCLW remainder-credit dispatch."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from src.config import config
from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.credit_transaction import CreditTransaction
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.services.liberclaw import LiberclawService
from src.services.payments.base import PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager
from src.services.payments.owner import Owner
from src.subscription_tiers import PRODUCT_LIBERCLAW
from tests.test_payment_manager import FakeProvider, _balance, _event_types, _make_user
from tests.test_payment_manager_liberclaw import _lc_user


@pytest.mark.asyncio
async def test_trial_to_paid_same_tier_supersedes(db):
    """A manual starter trial + start_checkout('starter') parks the trial as 'upgrading'
    (logged 'upgrade_started') and opens a fresh pending checkout instead of refusing."""
    account_id = uuid.uuid4()
    owner = Owner.for_liberclaw(account_id, email=f"{account_id.hex}@example.com")
    await _lc_user(db, account_id, tier="free")
    mgr = PaymentManager(FakeProvider(), db)

    trial = await mgr.grant_trial(owner, "starter", 14, granted_by="admin@example.com")

    result = await mgr.start_checkout(owner, tier="starter", redirect_url="http://x", currency="EUR")
    assert result.checkout_url

    refreshed_trial = await db.get(PlanSubscription, trial.id)
    assert refreshed_trial.status == "upgrading"
    assert "upgrade_started" in await _event_types(db, trial.id)

    pending = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.liberclaw_account_id == account_id, PlanSubscription.status == "pending"
            )
        )
    ).scalar_one()
    assert pending.tier == "starter"


@pytest.mark.asyncio
async def test_shared_dispatch_never_credits_trial_remainder(db):
    """A trial superseded by its own paid checkout must never generate an upgrade-remainder
    credit transaction — the shared dispatch skip runs before any product branch."""
    user = await _make_user(db)
    owner = Owner.for_user(user)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    trial = await mgr.grant_trial(owner, "go", 14, granted_by="admin@example.com")
    await mgr.start_checkout(owner, tier="go", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:trial_supersede",
            provider_subscription_id="psub_1",
            order_id="setup_1",
        )
    )

    refreshed_trial = await db.get(PlanSubscription, trial.id)
    assert refreshed_trial.status == "cancelled"
    assert "cancelled_for_upgrade" in await _event_types(db, trial.id)
    assert "upgrade_remainder_credited" not in await _event_types(db, trial.id)
    assert await _balance(db, user.id) == pytest.approx(0.0)

    tx_count = (
        (
            await db.execute(
                select(CreditTransaction).where(
                    CreditTransaction.external_reference == f"upgrade_remainder:{trial.id}"
                )
            )
        )
        .scalars()
        .all()
    )
    assert tx_count == []


@pytest.mark.asyncio
async def test_lclw_remainder_grants_lc_credits(db, monkeypatch):
    """A paid starter->pro LCLW upgrade grants the unused fraction of starter's credit cap
    as a liberclaw_credit_grants row, keyed 'upgrade_remainder:<old_sub_id>'."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    account_id = uuid.uuid4()
    owner = Owner.for_liberclaw(account_id, email=f"{account_id.hex}@example.com")
    await _lc_user(db, account_id, tier="free")
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.start_checkout(owner, tier="starter", redirect_url="http://x", currency="EUR")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:lclw_setup_1",
            provider_subscription_id="psub_1",
            order_id="setup_1",
        )
    )
    old_sub = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.liberclaw_account_id == account_id, PlanSubscription.status == "active"
            )
        )
    ).scalar_one()

    await mgr.upgrade(owner, new_tier="pro", redirect_url="http://x", currency="EUR")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:lclw_setup_2",
            provider_subscription_id="psub_2",
            order_id="setup_2",
        )
    )

    grant = (
        await db.execute(
            select(LiberclawCreditGrant).where(
                LiberclawCreditGrant.external_reference == f"upgrade_remainder:{old_sub.id}"
            )
        )
    ).scalar_one()
    expected = round(LIBERCLAW_TIERS["starter"]["credits_limit"] * (20 / 30), 2)
    assert grant.amount == pytest.approx(expected, abs=0.05)


@pytest.mark.asyncio
async def test_lclw_remainder_grant_failure_does_not_roll_back_activation(db, monkeypatch):
    """A DB-level failure inside the LCLW remainder grant (a real constraint violation, not a
    bare Python raise) must not poison the activation's transaction: the savepoint isolates it,
    the activation stands, and the retry sweep gets a pending event instead of a rolled-back
    webhook."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    account_id = uuid.uuid4()
    owner = Owner.for_liberclaw(account_id, email=f"{account_id.hex}@example.com")
    await _lc_user(db, account_id, tier="free")
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.start_checkout(owner, tier="starter", redirect_url="http://x", currency="EUR")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:fail_setup_1",
            provider_subscription_id="psub_1",
            order_id="setup_1",
        )
    )
    old_sub = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.liberclaw_account_id == account_id, PlanSubscription.status == "active"
            )
        )
    ).scalar_one()

    async def failing_grant(db_, account_id_, amount, external_reference):
        # A genuine DB error (CheckConstraint violation on amount > 0), not a bare raise —
        # this is what actually poisons the connection's transaction if left unhandled.
        lc_user = await LiberclawService.resolve_by_account_id(db_, account_id_)
        db_.add(LiberclawCreditGrant(liberclaw_user_id=lc_user.id, amount=-1, external_reference=external_reference))
        await db_.flush()
        return amount  # unreached

    monkeypatch.setattr(LiberclawService, "grant_extra_credits_by_account_id", failing_grant)

    await mgr.upgrade(owner, new_tier="pro", redirect_url="http://x", currency="EUR")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:fail_setup_2",
            provider_subscription_id="psub_2",
            order_id="setup_2",
        )
    )

    new_sub = (
        await db.execute(
            select(PlanSubscription).where(
                PlanSubscription.liberclaw_account_id == account_id, PlanSubscription.status == "active"
            )
        )
    ).scalar_one()
    assert new_sub.tier == "pro"  # activation stood despite the grant failure

    old_events = await _event_types(db, old_sub.id)
    assert "upgrade_remainder_pending" in old_events
    assert "upgrade_remainder_credited" not in old_events

    grant = (
        await db.execute(
            select(LiberclawCreditGrant).where(
                LiberclawCreditGrant.external_reference == f"upgrade_remainder:{old_sub.id}"
            )
        )
    ).scalar_one_or_none()
    assert grant is None  # the failed insert rolled back to the savepoint, not just failed silently


@pytest.mark.asyncio
async def test_override_tier_suppresses_upgrade_remainder(db):
    """An admin override never moves money: superseding a live paid LTAI sub must retire it
    without crediting any upgrade remainder for its unused time (balance stays at 0, not the
    ~2/3-of-a-cycle prepaid credit a real upgrade would have produced)."""
    user = await _make_user(db)
    owner = Owner.for_user(user)
    provider = FakeProvider()
    mgr = PaymentManager(provider, db)

    await mgr.start_checkout(owner, tier="go", redirect_url="http://x", currency="USD")
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:override_setup_1",
            provider_subscription_id="psub_1",
            order_id="setup_1",
        )
    )
    old_sub = (
        await db.execute(
            select(PlanSubscription).where(PlanSubscription.user_id == user.id, PlanSubscription.status == "active")
        )
    ).scalar_one()
    assert await _balance(db, user.id) == pytest.approx(0.0)

    await mgr.override_tier(owner, "plus")

    refreshed_old = await db.get(PlanSubscription, old_sub.id)
    assert refreshed_old.status == "cancelled"
    assert "upgrade_remainder_credited" not in await _event_types(db, old_sub.id)
    assert await _balance(db, user.id) == pytest.approx(0.0)  # not the ~5.33 a real upgrade would credit

    new_sub = (
        await db.execute(
            select(PlanSubscription).where(PlanSubscription.user_id == user.id, PlanSubscription.status == "active")
        )
    ).scalar_one()
    assert new_sub.tier == "plus"
    assert new_sub.provider == "manual"


@pytest.mark.asyncio
async def test_open_ended_manual_row_survives_expiry(db):
    """override_tier's open-ended manual row (current_period_end None) is never touched by
    check_expirations, no matter how old it gets."""
    user = await _make_user(db)
    owner = Owner.for_user(user)
    mgr = PaymentManager(FakeProvider(), db)

    await mgr.override_tier(owner, "go")
    sub = (await db.execute(select(PlanSubscription).where(PlanSubscription.user_id == user.id))).scalar_one()
    assert sub.current_period_end is None
    await db.execute(
        update(PlanSubscription)
        .where(PlanSubscription.id == sub.id)
        .values(updated_at=datetime.now() - timedelta(days=365))
    )

    await mgr.check_expirations()

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "active"
    assert refreshed.current_period_end is None


@pytest.mark.asyncio
async def test_lapsed_renewal_catchall_expires_active_and_overdue(db, monkeypatch):
    """A LCLW row whose renewal (or FINISHED) webhook never arrives is expired once it's well
    past RENEWAL_GRACE_DAYS, whether it was left 'active' or already went 'overdue' — LC's
    original rule catches both statuses, not just 'active'."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)

    account_active = uuid.uuid4()
    await _lc_user(db, account_active, tier="pro")
    active_sub = PlanSubscription(
        user_id=None,
        tier="pro",
        provider="fake",
        status="active",
        provider_subscription_id="psub_lapsed_active",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_active,
        current_period_end=datetime.now() - timedelta(days=8),
    )

    account_overdue = uuid.uuid4()
    await _lc_user(db, account_overdue, tier="pro")
    overdue_sub = PlanSubscription(
        user_id=None,
        tier="pro",
        provider="fake",
        status="overdue",
        provider_subscription_id="psub_lapsed_overdue",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_overdue,
        current_period_end=datetime.now() - timedelta(days=8),
    )
    db.add_all([active_sub, overdue_sub])
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    await mgr.check_expirations()

    refreshed_active = await db.get(PlanSubscription, active_sub.id)
    assert refreshed_active.status == "expired"
    refreshed_overdue = await db.get(PlanSubscription, overdue_sub.id)
    assert refreshed_overdue.status == "expired"

    lc_user_active = (
        await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_active))
    ).scalar_one()
    assert lc_user_active.tier == "free"
    lc_user_overdue = (
        await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_overdue))
    ).scalar_one()
    assert lc_user_overdue.tier == "free"


@pytest.mark.asyncio
async def test_stale_pending_expiry_is_provider_cancel_gated(db, monkeypatch):
    """A never-paid LCLW checkout past 24h is retired through _record_checkout_retired: if the
    provider cancel fails, the row stays pending (only the audit event is unconditional)."""
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_stale",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()
    await db.execute(
        update(PlanSubscription)
        .where(PlanSubscription.id == sub.id)
        .values(created_at=datetime.now() - timedelta(hours=25))
    )

    provider = FakeProvider()
    provider.cancel_failures.add("psub_stale")
    mgr = PaymentManager(provider, db)

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    await mgr.check_expirations()

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "pending"  # provider cancel failed: stays payable
    assert "expired_abandoned_checkout" in await _event_types(db, sub.id)


@pytest.mark.asyncio
async def test_stale_pending_expiry_retries_cancel_every_pass(db, monkeypatch):
    """A first failed provider cancel must not permanently strand a stale checkout as pending:
    the sweep retries the cancel on every pass, and only the audit event dedups."""
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_stale_retry",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()
    await db.execute(
        update(PlanSubscription)
        .where(PlanSubscription.id == sub.id)
        .values(created_at=datetime.now() - timedelta(hours=25))
    )

    provider = FakeProvider()
    provider.cancel_failures.add("psub_stale_retry")
    mgr = PaymentManager(provider, db)
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)

    # Pass 1: provider cancel fails — event logged, row stays pending.
    await mgr.check_expirations()
    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "pending"
    assert provider.cancelled == []
    assert (await _event_types(db, sub.id)).count("expired_abandoned_checkout") == 1

    # Pass 2: provider now accepts the cancel — must actually be retried, not skipped just
    # because the row already carries the retirement event.
    provider.cancel_failures.discard("psub_stale_retry")
    await mgr.check_expirations()

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "expired"
    assert provider.cancelled == ["psub_stale_retry"]
    assert (await _event_types(db, sub.id)).count("expired_abandoned_checkout") == 1  # never doubled


@pytest.mark.asyncio
async def test_resume_refuses_provider_cancelled(db):
    user = await _make_user(db)
    sub = PlanSubscription(
        user_id=user.id,
        tier="go",
        provider="fake",
        status="active",
        provider_subscription_id="psub_pc",
        cancel_at_period_end=True,
        provider_cancelled=True,
        current_period_end=datetime.now() + timedelta(days=5),
    )
    db.add(sub)
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    with pytest.raises(ValueError, match="already cancelled at the payment provider"):
        await mgr.resume(Owner.for_user(user))


@pytest.mark.asyncio
async def test_check_expirations_new_lclw_rules_scoped_by_flag(db, monkeypatch):
    """The new stale-pending-checkout expiry rule stays behind LIBERCLAW_BILLING_ENABLED like
    every other LCLW scope of check_expirations."""
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_stale_flag",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()
    await db.execute(
        update(PlanSubscription)
        .where(PlanSubscription.id == sub.id)
        .values(created_at=datetime.now() - timedelta(hours=25))
    )

    mgr = PaymentManager(FakeProvider(), db)

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    await mgr.check_expirations()
    await db.refresh(sub)
    assert sub.status == "pending"

    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    await mgr.check_expirations()
    await db.refresh(sub)
    assert sub.status == "expired"
