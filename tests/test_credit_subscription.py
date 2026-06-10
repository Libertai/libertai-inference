"""Credit-billed subscription service: unit tests.

Credits are seeded by adding ``CreditTransaction`` rows directly to the ``db``
fixture session. ``CreditService.get_balance`` / ``use_credits`` are called for
real with that same ``db`` session passed in, so the credit deduction and the
``PlanSubscription`` row live in one rolled-back transaction (real code path,
no monkeypatching).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.services.credit import CreditService
from src.services.payments.credit_subscription import CREDITS_PROVIDER, CreditSubscriptionService
from src.subscription_tiers import get_tier


# ------------------------------------------------------------------ helpers

async def _make_user(db: AsyncSession) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", email_verified=True)
    db.add(user)
    await db.flush()
    return user


async def _add_credits(db: AsyncSession, user_id: uuid.UUID, amount: float) -> None:
    """Seed a completed CreditTransaction directly on the db session."""
    tx = CreditTransaction(
        user_id=user_id,
        amount=amount,
        amount_left=amount,
        provider=CreditTransactionProvider.voucher,
        status=CreditTransactionStatus.completed,
        is_active=True,
    )
    db.add(tx)
    await db.flush()


async def _balance(db: AsyncSession, user_id: uuid.UUID) -> float:
    """Read credit balance from the same session (real CreditService, same db)."""
    return await CreditService.get_balance(user_id, db=db)


# ------------------------------------------------------------------ subscribe

async def test_subscribe_funded_creates_active_sub(db):
    user = await _make_user(db)
    price = get_tier("go").price_cents / 100  # 8.0
    await _add_credits(db, user.id, price + 5.0)

    sub = await CreditSubscriptionService.subscribe(db, user, "go")

    assert sub.status == "active"
    assert sub.provider == CREDITS_PROVIDER
    assert sub.tier == "go"
    assert sub.currency == "USD"
    assert sub.current_period_end is not None
    # Period is approximately now + 1 month.
    delta = (sub.current_period_end - sub.current_period_start).days
    assert 28 <= delta <= 32

    # Balance reduced by price.
    remaining = await _balance(db, user.id)
    assert abs(remaining - 5.0) < 0.01


async def test_subscribe_unfunded_raises_no_sub_created(db):
    user = await _make_user(db)
    # No credits seeded.

    with pytest.raises(ValueError, match="Insufficient credits"):
        await CreditSubscriptionService.subscribe(db, user, "go")

    # No subscription row should exist.
    count = (
        await db.execute(
            select(func.count()).select_from(PlanSubscription).where(PlanSubscription.user_id == user.id)
        )
    ).scalar()
    assert count == 0

    # Balance untouched (still 0).
    assert await _balance(db, user.id) == 0.0


async def test_subscribe_twice_raises_already_subscribed(db):
    user = await _make_user(db)
    price = get_tier("go").price_cents / 100
    await _add_credits(db, user.id, price * 3)

    await CreditSubscriptionService.subscribe(db, user, "go")
    with pytest.raises(ValueError, match="already have an active subscription"):
        await CreditSubscriptionService.subscribe(db, user, "go")


# ------------------------------------------------------------------ process_renewals

async def test_process_renewals_funded_renews(db):
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100
    await _add_credits(db, user.id, go_price * 3)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    balance_after_subscribe = await _balance(db, user.id)

    # Artificially push period end into the past.
    sub.current_period_start = datetime(past.year, past.month, 1)
    sub.current_period_end = past
    await db.flush()

    count = await CreditSubscriptionService.process_renewals(db, now=now)

    assert count == 1
    assert sub.status == "active"
    assert sub.current_period_end > now
    # Balance reduced again by go_price.
    remaining = await _balance(db, user.id)
    assert abs(remaining - (balance_after_subscribe - go_price)) < 0.01


async def test_process_renewals_unfunded_expires(db):
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100
    await _add_credits(db, user.id, go_price)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    # All credits spent on subscribe; balance now 0.
    assert await _balance(db, user.id) == pytest.approx(0.0, abs=0.01)

    sub.current_period_start = datetime(past.year, past.month, 1)
    sub.current_period_end = past
    await db.flush()

    count = await CreditSubscriptionService.process_renewals(db, now=now)

    assert count == 1
    assert sub.status == "expired"
    # Balance still 0 — no charge attempted.
    assert await _balance(db, user.id) == pytest.approx(0.0, abs=0.01)


async def test_process_renewals_just_below_price_expires_no_free_renewal(db):
    """TOCTOU guard: balance just below the renewal price -> the sub must expire,
    never renew 'for free'."""
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100  # 8.0

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    # Fund the first month, then leave just below the next month's price.
    await _add_credits(db, user.id, go_price + (go_price - 0.5))
    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    # Now balance is go_price - 0.5 (just below the renewal price).
    assert await _balance(db, user.id) == pytest.approx(go_price - 0.5, abs=0.01)

    balance_before = await _balance(db, user.id)

    sub.current_period_start = datetime(past.year, past.month, 1)
    sub.current_period_end = past
    await db.flush()

    count = await CreditSubscriptionService.process_renewals(db, now=now)

    assert count == 1
    assert sub.status == "expired"
    assert sub.tier == "go"
    # Pre-check catches it before any deduction -> balance unchanged.
    assert await _balance(db, user.id) == pytest.approx(balance_before, abs=0.01)


# ------------------------------------------------------------------ cancel + renewal

async def test_cancel_sets_flag_then_renewal_expires_no_charge(db):
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100
    await _add_credits(db, user.id, go_price * 3)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    result = await CreditSubscriptionService.cancel(db, user)
    assert result["effective_date"] is not None
    assert sub.cancel_at_period_end is True

    balance_after_cancel = await _balance(db, user.id)

    sub.current_period_start = datetime(past.year, past.month, 1)
    sub.current_period_end = past
    await db.flush()

    count = await CreditSubscriptionService.process_renewals(db, now=now)

    assert count == 1
    assert sub.status == "expired"
    # No additional charge on renewal.
    assert await _balance(db, user.id) == pytest.approx(balance_after_cancel, abs=0.01)


# ------------------------------------------------------------------ upgrade

async def test_upgrade_go_to_plus_charges_prorated_diff(db):
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100    # 8.0
    plus_price = get_tier("plus").price_cents / 100  # 20.0
    await _add_credits(db, user.id, 50.0)

    # Subscribe to go.
    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    bal_after_go = await _balance(db, user.id)

    # Upgrade immediately → frac ≈ 1.0 (period just started).
    original_end = sub.current_period_end
    await CreditSubscriptionService.upgrade(db, user, "plus")

    # Tier upgraded.
    assert sub.tier == "plus"
    assert sub.pending_tier is None

    # Charge ≈ (plus_price - go_price) * frac; frac ≈ 1 right after subscribe.
    expected_charge = plus_price - go_price  # ~12.0
    actual_charge = bal_after_go - await _balance(db, user.id)
    assert actual_charge == pytest.approx(expected_charge, rel=0.05)

    # Period end is unchanged.
    assert sub.current_period_end == original_end


async def test_upgrade_insufficient_credits_raises_no_change(db):
    """Upgrade with too few credits for the prorated diff -> raises, tier unchanged,
    no deduction."""
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100  # 8.0

    # Fund just enough for the go subscription, almost nothing left for an upgrade.
    await _add_credits(db, user.id, go_price + 0.5)
    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    balance_before = await _balance(db, user.id)  # 0.5

    with pytest.raises(ValueError, match="Insufficient credits"):
        await CreditSubscriptionService.upgrade(db, user, "plus")

    assert sub.tier == "go"
    assert sub.pending_tier is None
    # No deduction occurred.
    assert await _balance(db, user.id) == pytest.approx(balance_before, abs=0.01)


# ------------------------------------------------------------------ downgrade + renewal

async def test_downgrade_plus_to_go_then_renewal_charges_go_price(db):
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100  # 8.0
    await _add_credits(db, user.id, 60.0)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    sub = await CreditSubscriptionService.subscribe(db, user, "plus")
    result = await CreditSubscriptionService.request_downgrade(db, user, "go")
    assert result["new_tier"] == "go"
    assert sub.pending_tier == "go"

    balance_before_renewal = await _balance(db, user.id)

    sub.current_period_start = datetime(past.year, past.month, 1)
    sub.current_period_end = past
    await db.flush()

    count = await CreditSubscriptionService.process_renewals(db, now=now)

    assert count == 1
    assert sub.status == "active"
    assert sub.tier == "go"
    assert sub.pending_tier is None
    assert sub.current_period_end > now

    # Charged go_price on renewal.
    balance_after_renewal = await _balance(db, user.id)
    charge = balance_before_renewal - balance_after_renewal
    assert charge == pytest.approx(go_price, rel=0.01)


async def test_downgrade_unknown_tier_rejected(db):
    """An unknown tier must be rejected up front — a persisted bogus pending_tier
    would crash every subsequent renewal run."""
    user = await _make_user(db)
    await _add_credits(db, user.id, 30.0)
    await CreditSubscriptionService.subscribe(db, user, "plus")

    with pytest.raises(ValueError, match="Unknown tier"):
        await CreditSubscriptionService.request_downgrade(db, user, "bogus")


async def test_downgrade_free_then_paid_clears_cancel_flag(db):
    """Re-requesting a paid downgrade after a downgrade-to-free must clear the
    cancellation: renewal should move to the paid tier, not expire the sub."""
    user = await _make_user(db)
    await _add_credits(db, user.id, 60.0)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    sub = await CreditSubscriptionService.subscribe(db, user, "plus")
    await CreditSubscriptionService.request_downgrade(db, user, "free")
    assert sub.cancel_at_period_end is True

    await CreditSubscriptionService.request_downgrade(db, user, "go")
    assert sub.cancel_at_period_end is False
    assert sub.pending_tier == "go"

    sub.current_period_start = datetime(past.year, past.month, 1)
    sub.current_period_end = past
    await db.flush()

    count = await CreditSubscriptionService.process_renewals(db, now=now)

    assert count == 1
    assert sub.status == "active"
    assert sub.tier == "go"
    assert sub.pending_tier is None


async def test_renewal_poisoned_sub_does_not_block_batch(db):
    """A subscription with bad data (e.g. an unknown pending_tier persisted before
    validation existed) must not abort the whole renewal batch."""
    bad_user = await _make_user(db)
    good_user = await _make_user(db)
    await _add_credits(db, bad_user.id, 60.0)
    await _add_credits(db, good_user.id, 60.0)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    bad_sub = await CreditSubscriptionService.subscribe(db, bad_user, "plus")
    good_sub = await CreditSubscriptionService.subscribe(db, good_user, "go")
    # Simulate legacy poisoned data (request_downgrade now rejects this).
    bad_sub.pending_tier = "bogus"
    for sub in (bad_sub, good_sub):
        sub.current_period_start = datetime(past.year, past.month, 1)
        sub.current_period_end = past
    await db.flush()

    count = await CreditSubscriptionService.process_renewals(db, now=now)

    # Only the good sub was processed; the bad one is skipped, not fatal.
    assert count == 1
    assert good_sub.status == "active"
    assert good_sub.current_period_end > now
    assert bad_sub.status == "active"  # untouched, will be skipped again next run


async def test_renewal_anchors_period_at_previous_end(db):
    """A renewal shortly after period end starts the new period at the old end
    (no drift toward the cron run time)."""
    user = await _make_user(db)
    await _add_credits(db, user.id, 60.0)

    now = datetime.now()

    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    old_end = now - relativedelta(days=10)
    sub.current_period_start = old_end - relativedelta(months=1)
    sub.current_period_end = old_end
    await db.flush()

    count = await CreditSubscriptionService.process_renewals(db, now=now)

    assert count == 1
    assert sub.status == "active"
    assert sub.current_period_start == old_end
    assert sub.current_period_end == old_end + relativedelta(months=1)


async def test_upgrade_on_lapsed_period_renews_inline_then_charges_full_diff(db):
    """Upgrading while the period has lapsed (cron hasn't renewed yet) must not be
    free: the sub renews inline (old tier price), then the upgrade charges the
    (nearly) full tier difference against the fresh period."""
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100    # 8.0
    plus_price = get_tier("plus").price_cents / 100  # 20.0
    await _add_credits(db, user.id, 60.0)

    now = datetime.now()

    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    # Lapse the period by 30 minutes (within the hourly cron's blind spot).
    old_end = now - relativedelta(minutes=30)
    sub.current_period_start = old_end - relativedelta(months=1)
    sub.current_period_end = old_end
    await db.flush()

    balance_before = await _balance(db, user.id)
    await CreditSubscriptionService.upgrade(db, user, "plus")

    assert sub.tier == "plus"
    assert sub.status == "active"
    # Inline renewal anchored the fresh period at the old end.
    assert sub.current_period_start == old_end
    assert sub.current_period_end == old_end + relativedelta(months=1)

    # Charged: go renewal price + ~full prorated diff (period is ~30 min old).
    charge = balance_before - await _balance(db, user.id)
    assert charge == pytest.approx(go_price + (plus_price - go_price), rel=0.05)


async def test_upgrade_on_lapsed_cancelled_sub_expires_instead(db):
    """A cancelled sub whose period lapsed expires during the inline renewal —
    the upgrade is rejected, nothing is charged."""
    user = await _make_user(db)
    await _add_credits(db, user.id, 60.0)

    now = datetime.now()

    sub = await CreditSubscriptionService.subscribe(db, user, "go")
    await CreditSubscriptionService.cancel(db, user)
    old_end = now - relativedelta(minutes=30)
    sub.current_period_start = old_end - relativedelta(months=1)
    sub.current_period_end = old_end
    await db.flush()

    balance_before = await _balance(db, user.id)
    with pytest.raises(ValueError, match="expired"):
        await CreditSubscriptionService.upgrade(db, user, "plus")

    assert sub.status == "expired"
    assert await _balance(db, user.id) == pytest.approx(balance_before, abs=0.01)
