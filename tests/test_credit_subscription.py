"""Credit-billed subscription service: unit tests.

Credits are seeded by adding ``CreditTransaction`` rows directly to the ``db``
fixture session and patching ``CreditService.get_balance`` / ``use_credits`` to
query that same session so everything stays within the rolled-back transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
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
    """Read credit balance from the same session (mirrors CreditService.get_balance logic)."""
    result = await db.execute(
        select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
            CreditTransaction.user_id == user_id,
            CreditTransaction.is_active == True,  # noqa: E712
            CreditTransaction.status == CreditTransactionStatus.completed,
        )
    )
    return float(result.scalar() or 0.0)


def _make_credit_service_patches(db: AsyncSession):
    """Return context managers that patch CreditService to use *db* for get_balance/use_credits."""

    async def _get_balance(user_id: uuid.UUID) -> float:
        return await _balance(db, user_id)

    async def _use_credits(user_id: uuid.UUID, amount: float) -> bool:
        result = await db.execute(
            select(CreditTransaction)
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.is_active == True,  # noqa: E712
                CreditTransaction.status == CreditTransactionStatus.completed,
            )
            .order_by(
                CreditTransaction.expired_at.asc().nullslast(),
                CreditTransaction.created_at.asc(),
            )
        )
        txs = result.scalars().all()
        remaining = amount
        for tx in txs:
            available = tx.amount_left
            if available <= 0:
                continue
            take = min(available, remaining)
            tx.amount_left -= take
            remaining -= take
            if remaining <= 0:
                break
        return remaining <= 0

    p_balance = patch("src.services.payments.credit_subscription.CreditService.get_balance", side_effect=_get_balance)
    p_use = patch("src.services.payments.credit_subscription.CreditService.use_credits", side_effect=_use_credits)
    return p_balance, p_use


# ------------------------------------------------------------------ subscribe

async def test_subscribe_funded_creates_active_sub(db):
    user = await _make_user(db)
    price = get_tier("go").price_cents / 100  # 8.0
    await _add_credits(db, user.id, price + 5.0)

    p_balance, p_use = _make_credit_service_patches(db)
    with p_balance, p_use:
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

    p_balance, p_use = _make_credit_service_patches(db)
    with p_balance, p_use:
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

    p_balance, p_use = _make_credit_service_patches(db)
    with p_balance, p_use:
        await CreditSubscriptionService.subscribe(db, user, "go")
        with pytest.raises(ValueError, match="Already subscribed"):
            await CreditSubscriptionService.subscribe(db, user, "go")


# ------------------------------------------------------------------ process_renewals

async def test_process_renewals_funded_renews(db):
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100
    await _add_credits(db, user.id, go_price * 3)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    p_balance, p_use = _make_credit_service_patches(db)
    with p_balance, p_use:
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

    p_balance, p_use = _make_credit_service_patches(db)
    with p_balance, p_use:
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


# ------------------------------------------------------------------ cancel + renewal

async def test_cancel_sets_flag_then_renewal_expires_no_charge(db):
    user = await _make_user(db)
    go_price = get_tier("go").price_cents / 100
    await _add_credits(db, user.id, go_price * 3)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    p_balance, p_use = _make_credit_service_patches(db)
    with p_balance, p_use:
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

    p_balance, p_use = _make_credit_service_patches(db)
    with p_balance, p_use:
        # Subscribe to go.
        sub = await CreditSubscriptionService.subscribe(db, user, "go")
        bal_after_go = await _balance(db, user.id)

        # Upgrade immediately → frac ≈ 1.0 (period just started).
        await CreditSubscriptionService.upgrade(db, user, "plus")

    # Tier upgraded.
    assert sub.tier == "plus"
    assert sub.pending_tier is None
    # Period end unchanged.
    original_end = sub.current_period_end

    # Charge ≈ (plus_price - go_price) * frac; frac ≈ 1 right after subscribe.
    expected_charge = plus_price - go_price  # ~12.0
    actual_charge = bal_after_go - await _balance(db, user.id)
    assert actual_charge == pytest.approx(expected_charge, rel=0.05)

    # Period end is unchanged.
    assert sub.current_period_end == original_end


# ------------------------------------------------------------------ downgrade + renewal

async def test_downgrade_plus_to_go_then_renewal_charges_go_price(db):
    user = await _make_user(db)
    plus_price = get_tier("plus").price_cents / 100  # 20.0
    go_price = get_tier("go").price_cents / 100      # 8.0
    await _add_credits(db, user.id, 60.0)

    now = datetime.now()
    past = datetime(now.year - 1, now.month, now.day)

    p_balance, p_use = _make_credit_service_patches(db)
    with p_balance, p_use:
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
