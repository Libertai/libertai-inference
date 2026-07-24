import uuid
from datetime import date, datetime

from src.services.stats import StatsService


class FakeSub:
    def __init__(self, tier: str):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.tier = tier


class FakeEvent:
    def __init__(self, event_type: str, created_at: datetime, metadata: dict | None = None):
        self.event_type = event_type
        self.created_at = created_at
        self.metadata_json = metadata


def _tl(user_id, activated_on, ended_on=None, terminal_event=None):
    return {
        "user_id": user_id,
        "activated_on": activated_on,
        "ended_on": ended_on,
        "terminal_event": terminal_event,
        "tier_changes": [(activated_on, "go")] if activated_on else [],
    }


def test_churn_weekly_buckets_and_upgrade_exclusion():
    u_new = uuid.uuid4()
    u_churn = uuid.uuid4()
    u_upgrade = uuid.uuid4()
    timelines = [
        _tl(u_new, date(2026, 1, 5)),                                            # new in week of Jan 5
        _tl(u_churn, date(2025, 12, 1), date(2026, 1, 7), "cancelled"),          # churned week of Jan 5
        # upgrade pair, same week: old row terminated for upgrade + new row activated
        _tl(u_upgrade, date(2025, 12, 1), date(2026, 1, 6), "cancelled_for_upgrade"),
        _tl(u_upgrade, date(2026, 1, 6)),
    ]
    stats = StatsService._churn_from_timelines(timelines, date(2026, 1, 5), date(2026, 1, 11))
    assert len(stats.weekly) == 1
    week = stats.weekly[0]
    assert week.week_start == "2026-01-05"
    assert week.new == 1        # u_upgrade's re-activation excluded, u_new counted
    assert week.churned == 1    # cancelled_for_upgrade not churn; only u_churn
    assert week.net == 0
    assert stats.total_new == 1
    assert stats.total_churned == 1


def test_expired_insufficient_credits_ends_timeline_and_counts_as_churn():
    """A credits-provider sub that can't cover renewal expires via a distinct event type; the
    replay must still terminate its timeline (not count as live forever) and count it as churn."""
    sub = FakeSub(tier="go")
    timeline = StatsService._replay_subscription_timelines(
        [sub],
        {
            sub.id: [
                FakeEvent("activated", datetime(2026, 2, 1)),
                FakeEvent("expired_insufficient_credits", datetime(2026, 2, 15)),
            ]
        },
    )[0]
    assert timeline["ended_on"] == date(2026, 2, 15)
    assert timeline["terminal_event"] == "expired_insufficient_credits"

    stats = StatsService._churn_from_timelines([timeline], date(2026, 2, 1), date(2026, 2, 28))
    assert stats.total_new == 1
    assert stats.total_churned == 1
