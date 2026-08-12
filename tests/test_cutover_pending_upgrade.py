"""Cutover script: revives the paid row parked in ``upgrading``, retires unpaid checkouts."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from scripts.cutover_pending_upgrade import cutover
from tests.test_payment_manager import FakeProvider


async def _make_user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com")
    db.add(user)
    await db.flush()
    return user


def _fail_for(provider: FakeProvider, *ids: str) -> None:
    """Make the fake provider's cancel fail for specific subscription ids only."""
    original = provider.cancel_subscription

    async def maybe_fail(provider_subscription_id: str) -> None:
        if provider_subscription_id in ids:
            raise RuntimeError(f"provider down for {provider_subscription_id}")
        await original(provider_subscription_id)

    provider.cancel_subscription = maybe_fail  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_promotes_the_paid_parked_row_and_expires_the_rest(db):
    user = await _make_user(db)
    paid = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="upgrading",
        provider_subscription_id="psub_paid",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    )
    unpaid_parked = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="upgrading",
        provider_subscription_id="psub_parked",
    )
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending",
        provider_subscription_id="psub_checkout",
    )
    db.add_all([paid, unpaid_parked, checkout])
    await db.flush()

    counts = await cutover(db, FakeProvider())

    await db.refresh(paid)
    await db.refresh(unpaid_parked)
    await db.refresh(checkout)
    assert paid.status == "active"
    assert unpaid_parked.status == "expired"
    assert checkout.status == "expired"
    assert counts["promoted"] == 1


@pytest.mark.asyncio
async def test_expired_rows_carry_the_abandoned_checkout_event(db):
    """Plain `expired` would register as churn and disarm the activation refusal."""
    user = await _make_user(db)
    stale = PlanSubscription(
        user_id=user.id, tier="go", provider="fake", status="pending",
        provider_subscription_id="psub_stale",
    )
    db.add(stale)
    await db.flush()

    await cutover(db, FakeProvider())

    events = (await db.execute(
        select(PlanSubscriptionEvent.event_type).where(PlanSubscriptionEvent.subscription_id == stale.id)
    )).scalars().all()
    assert events == ["expired_abandoned_checkout"]


@pytest.mark.asyncio
async def test_user_with_a_live_row_is_not_promoted(db):
    user = await _make_user(db)
    live = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="active",
        provider_subscription_id="psub_live",
        current_period_start=datetime.now() - timedelta(days=1),
        current_period_end=datetime.now() + timedelta(days=29),
    )
    parked = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="upgrading",
        provider_subscription_id="psub_parked",
        current_period_start=datetime.now() - timedelta(days=40),
        current_period_end=datetime.now() - timedelta(days=10),
    )
    db.add_all([live, parked])
    await db.flush()

    counts = await cutover(db, FakeProvider())

    await db.refresh(live)
    await db.refresh(parked)
    assert live.status == "active"
    assert parked.status == "cancelled"  # given an explicit disposition, never left stranded
    assert counts["stranded"] == 0


@pytest.mark.asyncio
async def test_disposed_parked_row_credits_its_unused_remainder(db):
    """A parked row that loses the promote still holds paid-for time; cancelling it must not
    forfeit that, same as any other upgrade-away cancellation (`_supersede_other_subs`)."""
    user = await _make_user(db)
    live = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="active",
        provider_subscription_id="psub_live",
        current_period_start=datetime.now() - timedelta(days=1),
        current_period_end=datetime.now() + timedelta(days=29),
    )
    parked = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="upgrading",
        provider_subscription_id="psub_parked",
        current_period_start=datetime.now() - timedelta(days=15),
        current_period_end=datetime.now() + timedelta(days=15),  # half the cycle left
    )
    db.add_all([live, parked])
    await db.flush()

    await cutover(db, FakeProvider())

    await db.refresh(parked)
    assert parked.status == "cancelled"
    credited = (
        await db.execute(select(CreditTransaction).where(CreditTransaction.user_id == user.id))
    ).scalar_one()
    assert credited.amount > 0


@pytest.mark.asyncio
async def test_aborts_on_an_active_row_with_no_period_start(db):
    """`_refresh_cycle_dates` can write "active" without ever setting period dates; the
    null-start heuristic must never mistake that live row for an abandoned checkout."""
    user = await _make_user(db)
    live_but_unexplained = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="active",
        provider_subscription_id="psub_live",
    )
    db.add(live_but_unexplained)
    await db.flush()

    with pytest.raises(RuntimeError):
        await cutover(db, FakeProvider())

    await db.refresh(live_but_unexplained)
    assert live_but_unexplained.status == "active"


@pytest.mark.asyncio
async def test_a_failed_unpaid_cancel_strands_the_whole_user_not_just_that_row(db):
    """One unrelated checkout row failing to cancel must not cost the user their paid,
    parked subscription — nothing for that user is written until every cancel succeeds."""
    user = await _make_user(db)
    provider = FakeProvider()
    _fail_for(provider, "psub_checkout")

    paid = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="upgrading",
        provider_subscription_id="psub_paid",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    )
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending",
        provider_subscription_id="psub_checkout",
    )
    db.add_all([paid, checkout])
    await db.commit()  # a commit boundary cutover's internal rollback (on the failed user) must not cross

    counts = await cutover(db, provider)

    await db.refresh(paid)
    await db.refresh(checkout)
    assert paid.status == "upgrading"  # not promoted, not cancelled: left for a re-run
    assert checkout.status == "pending"
    assert counts["promoted"] == 0
    assert counts["expired"] == 0
    assert counts["stranded"] == 1


@pytest.mark.asyncio
async def test_a_null_end_parked_row_sorts_last(db):
    """current_period_end DESC NULLS LAST: a null end must never outrank a real one."""
    user = await _make_user(db)
    real_end = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="upgrading",
        provider_subscription_id="psub_real",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=1),
    )
    null_end = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="upgrading",
        provider_subscription_id="psub_null",
        current_period_start=datetime.now() - timedelta(days=1),
        current_period_end=None,
    )
    db.add_all([real_end, null_end])
    await db.flush()
    # null_end is created after real_end, so a `created_at` fallback for its missing end
    # would (wrongly) outrank real_end's near-future end date.

    counts = await cutover(db, FakeProvider())

    await db.refresh(real_end)
    await db.refresh(null_end)
    assert real_end.status == "active"
    assert null_end.status == "cancelled"
    assert counts["promoted"] == 1


@pytest.mark.asyncio
async def test_stranded_counts_a_parked_row_that_cannot_cancel(db):
    user = await _make_user(db)
    provider = FakeProvider()
    _fail_for(provider, "psub_loser")

    winner = PlanSubscription(
        user_id=user.id, tier="plus", provider="fake", status="upgrading",
        provider_subscription_id="psub_winner",
        current_period_start=datetime.now() - timedelta(days=5),
        current_period_end=datetime.now() + timedelta(days=25),
    )
    loser = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="upgrading",
        provider_subscription_id="psub_loser",
        current_period_start=datetime.now() - timedelta(days=40),
        current_period_end=datetime.now() - timedelta(days=10),
    )
    db.add_all([winner, loser])
    await db.flush()

    counts = await cutover(db, provider)

    await db.refresh(winner)
    await db.refresh(loser)
    assert winner.status == "active"
    assert loser.status == "upgrading"  # cancel failed: given no disposition, not stranded silently
    assert counts["stranded"] == 1


@pytest.mark.asyncio
async def test_pending_upgrade_row_is_swept_like_a_legacy_pending_row(db):
    user = await _make_user(db)
    checkout = PlanSubscription(
        user_id=user.id, tier="max", provider="fake", status="pending_upgrade",
        provider_subscription_id="psub_new_checkout",
    )
    db.add(checkout)
    await db.flush()

    counts = await cutover(db, FakeProvider())

    await db.refresh(checkout)
    assert checkout.status == "expired"
    assert counts["expired"] == 1


@pytest.mark.asyncio
async def test_dry_run_touches_nothing_and_never_calls_the_provider(db):
    user = await _make_user(db)
    provider = FakeProvider()
    checkout = PlanSubscription(
        user_id=user.id, tier="go", provider="fake", status="pending",
        provider_subscription_id="psub_stale",
    )
    db.add(checkout)
    await db.commit()  # a commit boundary the dry run's own rollback must not cross

    counts = await cutover(db, provider, dry_run=True)

    await db.refresh(checkout)
    assert checkout.status == "pending"
    assert provider.cancelled == []
    assert counts["expired"] == 1  # still reported, just never persisted
