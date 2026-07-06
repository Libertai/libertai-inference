from datetime import date

from src.interfaces.stats import UsersWindow
from src.services.stats import StatsService


def test_rolling_users_stats_week_window():
    # (day, identity) activity rows: u1 active Jan 1, u2 active Jan 3.
    rows = [
        (date(2026, 1, 1), "u1"),
        (date(2026, 1, 3), "u2"),
    ]
    stats = StatsService._rolling_users_stats(
        rows, start_date=date(2026, 1, 3), end_date=date(2026, 1, 5), window_days=7
    )
    # Jan 3: u1 (Jan 1 within trailing 7d) + u2 -> 2. Jan 4/5: still both within 7d -> 2.
    assert [(d.date, d.active_users) for d in stats.daily_active_users] == [
        ("2026-01-03", 2),
        ("2026-01-04", 2),
        ("2026-01-05", 2),
    ]
    # total_unique_users counts only activity INSIDE the requested range.
    assert stats.total_unique_users == 1  # only u2 acted within Jan 3-5


def test_rolling_users_stats_day_window_matches_plain_dau():
    rows = [(date(2026, 1, 1), "u1"), (date(2026, 1, 1), "u2"), (date(2026, 1, 2), "u1")]
    stats = StatsService._rolling_users_stats(
        rows, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), window_days=1
    )
    assert [(d.date, d.active_users) for d in stats.daily_active_users] == [
        ("2026-01-01", 2),
        ("2026-01-02", 1),
    ]
    assert stats.total_unique_users == 2


def test_users_window_enum_days():
    assert UsersWindow.day.days == 1
    assert UsersWindow.week.days == 7
    assert UsersWindow.month.days == 30
