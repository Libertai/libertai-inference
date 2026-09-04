import uuid
from datetime import date, datetime

from src.interfaces.stats import SubscriptionStatusFilter
from src.models.base import AsyncSessionLocal
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.services import stats as stats_module
from src.services.stats import StatsService
from src.subscription_tiers import PRODUCT_LIBERCLAW, PRODUCT_LIBERTAI


class FakeSub:
    def __init__(self, tier: str):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.tier = tier


class FakeEvent:
    def __init__(self, event_type: str, created_at: datetime):
        self.event_type = event_type
        self.created_at = created_at
        self.metadata_json = None


async def _seed_ltai_and_lclw_active_subs():
    """One active LTAI sub (user attached) + one active LCLW sub (no user, liberclaw owner)."""
    async with AsyncSessionLocal() as db:
        user = User(email=f"product-iso-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.flush()

        ltai_sub = PlanSubscription(user_id=user.id, tier="go", status="active", provider="revolut")
        lclw_sub = PlanSubscription(
            user_id=None,
            product=PRODUCT_LIBERCLAW,
            liberclaw_account_id=uuid.uuid4(),
            tier="starter",
            status="active",
            provider="revolut",
        )
        db.add_all([ltai_sub, lclw_sub])
        await db.commit()
        return ltai_sub, lclw_sub


async def test_get_latest_subscribers_ignores_liberclaw_rows_in_both_total_and_items():
    """`total` always equals `len(subscribers)`, and LCLW rows contribute to neither."""
    before = await StatsService.get_latest_subscribers(limit=None, statuses=[SubscriptionStatusFilter.active])

    await _seed_ltai_and_lclw_active_subs()

    after = await StatsService.get_latest_subscribers(limit=None, statuses=[SubscriptionStatusFilter.active])

    assert before.total == len(before.subscribers)
    assert after.total == len(after.subscribers)
    assert after.total == before.total + 1  # only the LTAI row counted, LCLW row excluded
    assert len(after.subscribers) == len(before.subscribers) + 1
    assert all(s.tier != "starter" for s in after.subscribers)  # LCLW tier never leaks through


async def test_get_global_subscriptions_revenue_excludes_liberclaw_tier_and_warning(monkeypatch):
    """LCLW rows never enter LTAI MRR: their tiers stay out of the per-tier breakdown and never
    reach `_tier_price`'s LTAI registry lookup, so no unknown-tier warning is emitted."""
    warnings: list[str] = []
    monkeypatch.setattr(stats_module.logger, "warning", lambda msg: warnings.append(msg))

    async with AsyncSessionLocal() as db:
        lclw_sub = PlanSubscription(
            user_id=None,
            product=PRODUCT_LIBERCLAW,
            liberclaw_account_id=uuid.uuid4(),
            tier="starter",
            status="active",
            provider="revolut",
        )
        db.add(lclw_sub)
        await db.flush()
        db.add(PlanSubscriptionEvent(subscription_id=lclw_sub.id, event_type="activated"))
        await db.commit()

    result = await StatsService.get_global_subscriptions_revenue(date(2020, 1, 1), date(2020, 1, 2))

    assert not any("starter" in w or "Unknown" in w or "unknown" in w for w in warnings)
    assert "starter" not in {m.tier for m in result.mrr_by_tier}
    assert "starter" not in {m.tier for m in result.credits_mrr_by_tier}


async def test_all_subscription_timelines_default_product_excludes_liberclaw():
    """LCLW rows have user_id=NULL (CheckConstraint enforces exactly one owner column set), so a
    correctly product-filtered LTAI timeline set never contains a None user_id."""
    await _seed_ltai_and_lclw_active_subs()

    async with AsyncSessionLocal() as db:
        timelines = await StatsService._all_subscription_timelines(db, product=PRODUCT_LIBERTAI)

    assert not any(t["user_id"] is None for t in timelines)


def test_expired_superseded_terminal_events_end_replay_and_are_not_live():
    """LC's terminal vocabulary (expired_superseded, expired_never_paid, expired_abandoned_upgrade)
    must end a replayed timeline exactly like the pre-existing terminal events -- otherwise a
    migrated dead LC sub replays as live MRR forever."""
    for event_name in ("expired_superseded", "expired_never_paid", "expired_abandoned_upgrade"):
        assert event_name in StatsService._TERMINAL_EVENTS
        assert event_name in StatsService._CHURN_TERMINAL_EVENTS

        sub = FakeSub(tier="starter")
        timeline = StatsService._replay_subscription_timelines(
            [sub],
            {
                sub.id: [
                    FakeEvent("activated", datetime(2026, 2, 1)),
                    FakeEvent(event_name, datetime(2026, 2, 15)),
                ]
            },
        )[0]
        assert timeline["ended_on"] == date(2026, 2, 15)
        assert timeline["terminal_event"] == event_name
        assert StatsService._tier_at(timeline, date(2026, 2, 15)) is None  # not live on/after ended_on
        assert StatsService._tier_at(timeline, date(2026, 3, 1)) is None
