import uuid
from datetime import date

from src.services.stats import StatsService


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
