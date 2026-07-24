from datetime import datetime

from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.interfaces.stats import SubscriptionActivityType, SubscriptionStatusFilter
from src.services.stats import StatsService


async def test_latest_subscribers_newest_first_with_user_label():
    # created_at defaults to CURRENT_TIMESTAMP, which is constant within one transaction, so both
    # rows would otherwise tie on ordering — set explicit, distinct timestamps like other tests do
    # (e.g. test_stats_subscriptions.py's `churned.updated_at = ts`).
    async with AsyncSessionLocal() as db:
        u1 = User(email="sub1@example.com")
        u2 = User(email="sub2@example.com")
        db.add_all([u1, u2])
        await db.flush()
        s1 = PlanSubscription(user_id=u1.id, tier="go", status="active", provider="revolut")
        s1.created_at = datetime(2099, 1, 1)
        s2 = PlanSubscription(user_id=u2.id, tier="plus", status="cancelled", provider="revolut")
        s2.created_at = datetime(2099, 1, 2)
        db.add_all([s1, s2])
        await db.commit()

    stats = await StatsService.get_latest_subscribers(limit=1)
    assert len(stats.subscribers) == 1
    latest = stats.subscribers[0]
    assert latest.user_label == "sub2@example.com"  # inserted last -> newest created_at
    assert latest.tier == "plus"
    assert latest.status == "cancelled"


async def test_latest_subscribers_status_filtering():
    async with AsyncSessionLocal() as db:
        u1 = User(email="statusfilter1@example.com")
        u2 = User(email="statusfilter2@example.com")
        db.add_all([u1, u2])
        await db.flush()
        pending = PlanSubscription(user_id=u1.id, tier="go", status="pending", provider="revolut")
        pending.created_at = datetime(2099, 2, 2)
        active = PlanSubscription(user_id=u2.id, tier="plus", status="active", provider="revolut")
        active.created_at = datetime(2099, 2, 1)
        db.add_all([pending, active])
        await db.commit()

    # Default: pending excluded.
    default = await StatsService.get_latest_subscribers(limit=5)
    assert all(s.status != "pending" for s in default.subscribers)

    # all: pending included (it's the newest row overall).
    everything = await StatsService.get_latest_subscribers(limit=5, statuses=[SubscriptionStatusFilter.all])
    assert everything.subscribers[0].status == "pending"

    # Exact status filter.
    only_pending = await StatsService.get_latest_subscribers(limit=5, statuses=[SubscriptionStatusFilter.pending])
    assert only_pending.subscribers and all(s.status == "pending" for s in only_pending.subscribers)

    # Multi-status set: rows whose status is in the set.
    subset = await StatsService.get_latest_subscribers(
        limit=5, statuses=[SubscriptionStatusFilter.pending, SubscriptionStatusFilter.active]
    )
    got = {s.status for s in subset.subscribers}
    assert got == {"pending", "active"}


async def test_latest_subscribers_total_counts_all_matching_ignoring_limit():
    async with AsyncSessionLocal() as db:
        for i in range(3):
            u = User(email=f"totalcount{i}@example.com")
            db.add(u)
            await db.flush()
            sub = PlanSubscription(user_id=u.id, tier="go", status="active", provider="revolut")
            sub.created_at = datetime(2099, 3, i + 1)
            db.add(sub)
        await db.commit()

    limited = await StatsService.get_latest_subscribers(limit=1, statuses=[SubscriptionStatusFilter.active])
    assert len(limited.subscribers) == 1
    assert limited.total >= 3  # counts every active row, not just the returned page

    unlimited = await StatsService.get_latest_subscribers(limit=None, statuses=[SubscriptionStatusFilter.active])
    assert len(unlimited.subscribers) == limited.total


def _event(sub_id, kind, day, pid=None, metadata=None):
    e = PlanSubscriptionEvent(subscription_id=sub_id, event_type=kind, provider_event_id=pid, metadata_json=metadata)
    e.created_at = datetime(2099, 4, day)
    return e


async def test_subscription_activity_maps_drops_noise_and_keeps_churn():
    async with AsyncSessionLocal() as db:
        subscriber = User(email="activity-sub@example.com")
        abandoner = User(email="activity-abandon@example.com")
        db.add_all([subscriber, abandoner])
        await db.flush()

        live = PlanSubscription(user_id=subscriber.id, tier="plus", status="cancelled", provider="revolut")
        live.created_at = datetime(2099, 4, 1)
        dead = PlanSubscription(user_id=abandoner.id, tier="go", status="expired", provider="revolut")
        dead.created_at = datetime(2099, 4, 1)
        db.add_all([live, dead])
        await db.flush()

        db.add_all(
            [
                # created/initiated are intent noise -> dropped; activated -> Subscribed.
                _event(live.id, "created", 1, "n-1", {"tier": "plus"}),
                _event(live.id, "activated", 2, "n-2"),
                _event(live.id, "payment_failed", 3, "n-3"),
                _event(live.id, "cancelled", 4, "n-4"),
                _event(live.id, "expired", 5, "n-5"),  # real end -> Churned
                # Abandoned checkout uses its own event type -> dropped entirely.
                _event(dead.id, "created", 1, "n-6", {"tier": "go"}),
                _event(dead.id, "expired_abandoned_checkout", 2, "n-7"),
            ]
        )
        await db.commit()

    result = await StatsService.get_subscription_activity(limit=50)
    live_events = [e for e in result.events if e.user_label == "activity-sub@example.com"]
    dead_events = [e for e in result.events if e.user_label == "activity-abandon@example.com"]
    types = {e.type for e in live_events}

    assert types == {
        SubscriptionActivityType.subscribed,
        SubscriptionActivityType.payment_failed,
        SubscriptionActivityType.cancelled,
        SubscriptionActivityType.churned,
    }
    assert dead_events == []  # created + expired_abandoned_checkout both dropped
    assert result.events == sorted(result.events, key=lambda e: e.created_at, reverse=True)

    only_cancel = await StatsService.get_subscription_activity(limit=50, types=[SubscriptionActivityType.cancelled])
    assert all(e.type is SubscriptionActivityType.cancelled for e in only_cancel.events)


async def test_subscription_activity_collapses_upgrade_into_one_row():
    async with AsyncSessionLocal() as db:
        user = User(email="activity-upgrade@example.com")
        db.add(user)
        await db.flush()

        old = PlanSubscription(user_id=user.id, tier="go", status="cancelled", provider="revolut")
        old.created_at = datetime(2099, 4, 1)
        new = PlanSubscription(user_id=user.id, tier="plus", status="active", provider="revolut")
        new.created_at = datetime(2099, 4, 2)
        db.add_all([old, new])
        await db.flush()

        db.add_all(
            [
                _event(old.id, "upgrading", 2, "u-1", {"new_tier": "plus"}),
                _event(old.id, "cancelled_for_upgrade", 3, "u-2"),
                _event(new.id, "activated", 3, "u-3"),
                _event(new.id, "upgraded", 3, "u-4", {"from": "go", "to": "plus"}),
            ]
        )
        await db.commit()

    result = await StatsService.get_subscription_activity(limit=50)
    mine = [e for e in result.events if e.user_label == "activity-upgrade@example.com"]

    # One row: Upgraded go -> plus. The new sub's "Subscribed" is suppressed; the old sub's
    # upgrading/cancelled_for_upgrade bookkeeping is dropped.
    assert len(mine) == 1
    assert mine[0].type is SubscriptionActivityType.upgraded
    assert mine[0].from_tier == "go"
    assert mine[0].tier == "plus"


async def test_subscription_activity_offset_paginates_mapped_stream():
    full = await StatsService.get_subscription_activity(limit=200)
    assert full.total == len(full.events)
    assert full.total >= 2  # earlier tests committed several mapped events

    page1 = await StatsService.get_subscription_activity(limit=1, offset=0)
    page2 = await StatsService.get_subscription_activity(limit=1, offset=1)
    assert page1.total == page2.total == full.total  # total ignores the page window
    assert [*page1.events, *page2.events] == full.events[:2]

    past_end = await StatsService.get_subscription_activity(limit=1, offset=full.total)
    assert past_end.events == []


async def test_subscription_activity_distinguishes_renewals():
    async with AsyncSessionLocal() as db:
        renewer = User(email="activity-renew@example.com")
        db.add(renewer)
        await db.flush()
        sub = PlanSubscription(user_id=renewer.id, tier="go", status="active", provider="revolut")
        sub.created_at = datetime(2099, 4, 10)
        db.add(sub)
        await db.flush()
        db.add_all(
            [
                _event(sub.id, "created", 10, "r-1", {"tier": "go"}),
                _event(sub.id, "activated", 11, "r-2"),
                _event(sub.id, "renewed", 12, "r-3"),
            ]
        )
        await db.commit()

    result = await StatsService.get_subscription_activity(limit=50)
    mine = [e for e in result.events if e.user_label == "activity-renew@example.com"]
    assert [e.type for e in mine] == [SubscriptionActivityType.renewed, SubscriptionActivityType.subscribed]
    assert all(e.tier == "go" for e in mine)

    only_renewed = await StatsService.get_subscription_activity(
        limit=50, types=[SubscriptionActivityType.renewed]
    )
    assert only_renewed.events and all(
        e.type is SubscriptionActivityType.renewed for e in only_renewed.events
    )
    assert any(e.user_label == "activity-renew@example.com" for e in only_renewed.events)


async def test_subscription_activity_shows_renewals_of_upgraded_subs():
    # The upgrade-completion "activated" stays suppressed (its "upgraded" row represents it),
    # but later renewals of that sub must still appear.
    async with AsyncSessionLocal() as db:
        user = User(email="activity-upgrenew@example.com")
        db.add(user)
        await db.flush()
        sub = PlanSubscription(user_id=user.id, tier="plus", status="active", provider="revolut")
        sub.created_at = datetime(2099, 4, 20)
        db.add(sub)
        await db.flush()
        db.add_all(
            [
                _event(sub.id, "activated", 20, "ur-1"),
                _event(sub.id, "upgraded", 20, "ur-2", {"from": "go", "to": "plus"}),
                _event(sub.id, "renewed", 21, "ur-3"),
            ]
        )
        await db.commit()

    result = await StatsService.get_subscription_activity(limit=50)
    mine = [e for e in result.events if e.user_label == "activity-upgrenew@example.com"]
    assert [e.type for e in mine] == [SubscriptionActivityType.renewed, SubscriptionActivityType.upgraded]


async def test_subscription_activity_shows_churn_for_expired_insufficient_credits():
    # A credits-provider sub that can't cover renewal expires via a distinct event type; it must
    # still surface as a churn in the feed rather than being dropped or misclassified.
    async with AsyncSessionLocal() as db:
        user = User(email="activity-insufficient@example.com")
        db.add(user)
        await db.flush()
        sub = PlanSubscription(user_id=user.id, tier="go", status="expired", provider="credits")
        sub.created_at = datetime(2099, 4, 25)
        db.add(sub)
        await db.flush()
        db.add_all(
            [
                _event(sub.id, "activated", 25, "ic-1"),
                _event(sub.id, "expired_insufficient_credits", 26, "ic-2"),
            ]
        )
        await db.commit()

    result = await StatsService.get_subscription_activity(limit=50)
    mine = [e for e in result.events if e.user_label == "activity-insufficient@example.com"]
    assert [e.type for e in mine] == [SubscriptionActivityType.churned, SubscriptionActivityType.subscribed]


# Runs last and asserts membership only, so its committed rows can't perturb the order-dependent
# assertions above (these tests share one session-scoped DB with no per-test rollback).
async def test_latest_subscribers_label_prefers_display_name_with_contact_in_parens():
    async with AsyncSessionLocal() as db:
        named = User(email="named@example.com", display_name="Alice")
        wallet = User(display_name="Bob", address="0xWALLET")
        anon = User(email="anon@example.com")
        db.add_all([named, wallet, anon])
        await db.flush()
        for i, u in enumerate([named, wallet, anon]):
            sub = PlanSubscription(user_id=u.id, tier="go", status="active", provider="revolut")
            sub.created_at = datetime(2099, 5, i + 1)
            db.add(sub)
        await db.commit()

    labels = {
        s.user_label
        for s in (
            await StatsService.get_latest_subscribers(limit=None, statuses=[SubscriptionStatusFilter.active])
        ).subscribers
    }
    assert "Alice (named@example.com)" in labels  # display_name + email contact
    assert "Bob (0xWALLET)" in labels  # no email -> wallet address as contact
    assert "anon@example.com" in labels  # no display_name -> bare contact
