from datetime import date, datetime, timedelta

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.stats import UsersWindow
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.services.stats import StatsService
from src.services.users import get_or_create_user_by_wallet

D = date(2020, 6, 10)
PRE_RANGE = datetime.combine(D - timedelta(days=3), datetime.min.time().replace(hour=12))
IN_RANGE = datetime.combine(D, datetime.min.time().replace(hour=12))

U1 = "0xDA0200000000000000000000000000000000B001"
U2 = "0xDA0200000000000000000000000000000000B002"


def _inference_call(api_key_id, when: datetime) -> InferenceCall:
    call = InferenceCall(api_key_id=api_key_id, credits_used=0.0, model_name="test-model")
    call.used_at = when
    return call


async def _seed_fetch_start_extension() -> None:
    async with AsyncSessionLocal() as db:
        user1 = await get_or_create_user_by_wallet(db, U1)
        user2 = await get_or_create_user_by_wallet(db, U2)
        await db.flush()

        u1_api = ApiKey(key=ApiKey.generate_key(), name="fse-u1-api", user_id=user1.id, type=ApiKeyType.api)
        u2_api = ApiKey(key=ApiKey.generate_key(), name="fse-u2-api", user_id=user2.id, type=ApiKeyType.api)
        db.add_all([u1_api, u2_api])
        await db.flush()

        db.add_all(
            [
                _inference_call(u1_api.id, PRE_RANGE),  # u1: D-3, before the queried range
                _inference_call(u2_api.id, IN_RANGE),  # u2: D, inside the queried range
            ]
        )
        await db.commit()


async def test_get_inference_users_stats_fetch_start_extension():
    await _seed_fetch_start_extension()

    week = await StatsService._get_inference_users_stats(
        ApiKeyType.api, start_date=D, end_date=D + timedelta(days=1), window=UsersWindow.week
    )
    by_day = {d.date: d.active_users for d in week.daily_active_users}
    # D's trailing 7-day window reaches back to D-3, so both u1 (pre-range) and u2 count.
    assert by_day[D.strftime("%Y-%m-%d")] == 2
    # total_unique_users only counts activity inside [D, D+1] -> just u2.
    assert week.total_unique_users == 1

    day = await StatsService._get_inference_users_stats(
        ApiKeyType.api, start_date=D, end_date=D + timedelta(days=1), window=UsersWindow.day
    )
    day_by_day = {d.date: d.active_users for d in day.daily_active_users}
    # No fetch-start extension -> pre-range u1 not counted on D.
    assert day_by_day[D.strftime("%Y-%m-%d")] == 1


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
