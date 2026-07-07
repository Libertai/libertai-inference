from datetime import datetime

from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.interfaces.stats import SubscriptionStatusFilter
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
    everything = await StatsService.get_latest_subscribers(limit=5, status=SubscriptionStatusFilter.all)
    assert everything.subscribers[0].status == "pending"

    # Exact status filter.
    only_pending = await StatsService.get_latest_subscribers(limit=5, status=SubscriptionStatusFilter.pending)
    assert only_pending.subscribers and all(s.status == "pending" for s in only_pending.subscribers)
