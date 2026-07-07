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


async def test_subscription_activity_maps_collapses_and_hides_abandoned():
    async with AsyncSessionLocal() as db:
        subscriber = User(email="activity-sub@example.com")
        abandoner = User(email="activity-abandon@example.com")
        db.add_all([subscriber, abandoner])
        await db.flush()

        # A real subscription: created + activated (collapse to one Subscribed) then cancelled.
        live = PlanSubscription(user_id=subscriber.id, tier="plus", status="cancelled", provider="revolut")
        live.created_at = datetime(2099, 4, 1)
        # An abandoned checkout: created then expired, never activated -> churn hidden.
        dead = PlanSubscription(user_id=abandoner.id, tier="go", status="expired", provider="revolut")
        dead.created_at = datetime(2099, 4, 1)
        db.add_all([live, dead])
        await db.flush()

        def ev(sub_id, kind, day, pid):
            e = PlanSubscriptionEvent(subscription_id=sub_id, event_type=kind, provider_event_id=pid)
            e.created_at = datetime(2099, 4, day)
            return e

        db.add_all(
            [
                ev(live.id, "created", 1, "act-1"),
                ev(live.id, "activated", 2, "act-2"),
                ev(live.id, "cancelled", 3, "act-3"),
                ev(dead.id, "created", 1, "act-4"),
                ev(dead.id, "expired", 2, "act-5"),
            ]
        )
        await db.commit()

    result = await StatsService.get_subscription_activity(limit=50)
    mine = [e for e in result.events if e.user_label in {"activity-sub@example.com", "activity-abandon@example.com"}]
    types = [e.type for e in mine]

    # created dropped, activated -> Subscribed, cancelled kept; abandoned expired hidden.
    assert SubscriptionActivityType.subscribed in types
    assert SubscriptionActivityType.cancelled in types
    assert SubscriptionActivityType.churned not in types
    assert types.count(SubscriptionActivityType.subscribed) == 1  # created+activated collapsed
    # Newest first.
    assert mine == sorted(mine, key=lambda e: e.created_at, reverse=True)

    # Type filter narrows the feed.
    only_cancel = await StatsService.get_subscription_activity(limit=50, types=[SubscriptionActivityType.cancelled])
    assert all(e.type is SubscriptionActivityType.cancelled for e in only_cancel.events)
