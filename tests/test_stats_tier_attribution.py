import uuid
from datetime import date, datetime

from src.services.stats import StatsService


class FakeSub:
    def __init__(self, tier: str, user_id: uuid.UUID | None = None):
        self.id = uuid.uuid4()
        self.user_id = user_id or uuid.uuid4()
        self.tier = tier


class FakeEvent:
    def __init__(self, event_type: str, created_at: datetime, metadata: dict | None = None):
        self.event_type = event_type
        self.created_at = created_at
        self.metadata_json = metadata


def _timelines(pairs):
    """pairs: [(FakeSub, [FakeEvent, ...]), ...]"""
    subs = [s for s, _ in pairs]
    events = {s.id: e for s, e in pairs}
    return StatsService._replay_subscription_timelines(subs, events)


def test_tier_by_user_day_maps_active_users_only():
    sub = FakeSub("plus")
    timelines = _timelines(
        [(sub, [FakeEvent("created", datetime(2026, 6, 1), {"tier": "plus"}),
                FakeEvent("activated", datetime(2026, 6, 2))])]
    )
    assert StatsService._tier_by_user_day(timelines, date(2026, 6, 1)) == {}
    assert StatsService._tier_by_user_day(timelines, date(2026, 6, 2)) == {sub.user_id: "plus"}


def test_upgrade_splits_credits_across_go_and_plus():
    """A user on Go who upgrades to Plus must have their Go-era credits land on Go,
    not be retroactively credited to Plus. This is THE regression guard."""
    user = uuid.uuid4()
    go_sub = FakeSub("go", user_id=user)
    plus_sub = FakeSub("plus", user_id=user)
    timelines = _timelines([
        (go_sub, [FakeEvent("created", datetime(2026, 6, 1), {"tier": "go"}),
                  FakeEvent("activated", datetime(2026, 6, 1)),
                  FakeEvent("cancelled_for_upgrade", datetime(2026, 6, 3))]),
        (plus_sub, [FakeEvent("created", datetime(2026, 6, 3), {"tier": "plus"}),
                    FakeEvent("activated", datetime(2026, 6, 3))]),
    ])
    credits = [
        (date(2026, 6, 1), user, 1.0),  # on Go
        (date(2026, 6, 2), user, 2.0),  # on Go
        (date(2026, 6, 3), user, 4.0),  # upgraded -> Plus
        (date(2026, 6, 4), user, 8.0),  # on Plus
    ]
    totals = StatsService._aggregate_credits_by_tier(
        credits, timelines, date(2026, 6, 1), date(2026, 6, 4)
    )
    assert totals[(date(2026, 6, 1), "go")] == 1.0
    assert totals[(date(2026, 6, 2), "go")] == 2.0
    assert totals[(date(2026, 6, 3), "plus")] == 4.0
    assert totals[(date(2026, 6, 4), "plus")] == 8.0
    assert (date(2026, 6, 4), "go") not in totals


def test_credits_with_no_active_sub_bucket_into_free():
    user = uuid.uuid4()
    totals = StatsService._aggregate_credits_by_tier(
        [(date(2026, 6, 5), user, 3.0)], [], date(2026, 6, 5), date(2026, 6, 5)
    )
    assert totals == {(date(2026, 6, 5), "free"): 3.0}


def test_aggregate_ignores_credits_outside_range():
    user = uuid.uuid4()
    totals = StatsService._aggregate_credits_by_tier(
        [(date(2026, 5, 30), user, 3.0), (date(2026, 6, 5), user, 1.0)],
        [], date(2026, 6, 1), date(2026, 6, 5),
    )
    assert totals == {(date(2026, 6, 5), "free"): 1.0}


def test_subscribers_by_tier_day_dedupes_users_and_skips_empty():
    user = uuid.uuid4()
    a = FakeSub("plus", user_id=user)
    b = FakeSub("plus", user_id=user)  # same human, two sub rows
    timelines = _timelines([
        (a, [FakeEvent("created", datetime(2026, 6, 1), {"tier": "plus"}),
             FakeEvent("activated", datetime(2026, 6, 1))]),
        (b, [FakeEvent("created", datetime(2026, 6, 1), {"tier": "plus"}),
             FakeEvent("activated", datetime(2026, 6, 1))]),
    ])
    counts = StatsService._subscribers_by_tier_day(timelines, date(2026, 5, 31), date(2026, 6, 1))
    assert counts == {(date(2026, 6, 1), "plus"): 1}  # no (5/31, plus) key at all
