import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import delete

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
from src.services.auth_tokens import create_access_token
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


def _timeline(sub, events):
    return StatsService._replay_subscription_timelines([sub], {sub.id: events})[0]


def test_replay_basic_lifecycle():
    sub = FakeSub(tier="plus")
    t = _timeline(
        sub,
        [
            FakeEvent("created", datetime(2026, 1, 1), {"tier": "plus"}),
            FakeEvent("activated", datetime(2026, 1, 2)),
            FakeEvent("activated", datetime(2026, 2, 2)),  # renewal: window start = FIRST activated
            FakeEvent("cancelled", datetime(2026, 3, 10)),
        ],
    )
    assert t["activated_on"] == date(2026, 1, 2)
    assert t["ended_on"] == date(2026, 3, 10)
    assert t["tier_changes"] == [(date(2026, 1, 2), "plus")]


def test_replay_downgrade_changes_tier_at_event_time():
    sub = FakeSub(tier="go")  # current row tier is post-downgrade
    t = _timeline(
        sub,
        [
            FakeEvent("created", datetime(2026, 1, 1), {"tier": "plus"}),
            FakeEvent("activated", datetime(2026, 1, 1)),
            FakeEvent("downgraded", datetime(2026, 2, 1), {"from": "plus", "to": "go"}),
        ],
    )
    assert t["ended_on"] is None
    assert t["tier_changes"] == [(date(2026, 1, 1), "plus"), (date(2026, 2, 1), "go")]


def test_replay_upgrade_termination_and_never_activated():
    old = FakeSub(tier="go")
    t_old = _timeline(
        old,
        [
            FakeEvent("created", datetime(2026, 1, 1), {"tier": "go"}),
            FakeEvent("activated", datetime(2026, 1, 1)),
            FakeEvent("cancelled_for_upgrade", datetime(2026, 2, 1)),
        ],
    )
    assert t_old["ended_on"] == date(2026, 2, 1)

    abandoned = FakeSub(tier="max")
    t_ab = _timeline(abandoned, [FakeEvent("created", datetime(2026, 1, 5), {"tier": "max"})])
    assert t_ab["activated_on"] is None  # contributes 0 MRR


def test_mrr_from_timelines():
    # go = $8, plus = $20 (src/subscription_tiers.py)
    sub = FakeSub(tier="go")
    timelines = [
        {
            "user_id": sub.user_id,
            "activated_on": date(2026, 1, 1),
            "ended_on": date(2026, 1, 3),
            "tier_changes": [(date(2026, 1, 1), "plus")],
        }
    ]
    daily = StatsService._mrr_daily(timelines, date(2026, 1, 1), date(2026, 1, 4))
    assert [(d.date, d.mrr) for d in daily] == [
        ("2026-01-01", 20.0),
        ("2026-01-02", 20.0),
        ("2026-01-03", 0.0),   # ended_on is exclusive: terminated that day
        ("2026-01-04", 0.0),
    ]


def test_mrr_upgrade_pair_no_double_count():
    # same user: old "go" sub cancelled_for_upgrade on the upgrade day, new "max" sub activated that same day.
    user_id = uuid.uuid4()
    timelines = [
        {
            "user_id": user_id,
            "activated_on": date(2026, 1, 1),
            "ended_on": date(2026, 1, 10),
            "tier_changes": [(date(2026, 1, 1), "go")],
        },
        {
            "user_id": user_id,
            "activated_on": date(2026, 1, 10),
            "ended_on": None,
            "tier_changes": [(date(2026, 1, 10), "max")],
        },
    ]
    daily = StatsService._mrr_daily(timelines, date(2026, 1, 9), date(2026, 1, 11))
    assert [(d.date, d.mrr) for d in daily] == [
        ("2026-01-09", 8.0),
        ("2026-01-10", 100.0),  # old row's ended_on is exclusive: contributes 0 on the upgrade day
        ("2026-01-11", 100.0),
    ]


def test_mrr_downgrade_dollar_amounts():
    sub = FakeSub(tier="go")
    timelines = [
        {
            "user_id": sub.user_id,
            "activated_on": date(2026, 1, 1),
            "ended_on": None,
            "tier_changes": [(date(2026, 1, 1), "plus"), (date(2026, 2, 1), "go")],
        }
    ]
    daily = StatsService._mrr_daily(timelines, date(2026, 1, 31), date(2026, 2, 1))
    assert [(d.date, d.mrr) for d in daily] == [
        ("2026-01-31", 20.0),
        ("2026-02-01", 8.0),
    ]


def test_topups_window_starts_at_first_of_month():
    assert StatsService._topups_window_start(date(2026, 7, 5)) == date(2026, 7, 1)
    assert StatsService._topups_window_start(date(2026, 7, 1)) == date(2026, 7, 1)
    assert StatsService._topups_window_start(date(2026, 12, 31)) == date(2026, 12, 1)


async def _mk_staff_headers(db) -> dict:
    u = User(email=f"revenue-staff-{uuid.uuid4().hex}@example.com")
    u.is_libertai_staff = True
    db.add(u)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(u.id)}"}


@pytest.mark.asyncio
async def test_topups_daily_widens_but_total_topups_stays_in_range(async_client):
    """topups_daily reaches back to the 1st of start_date's month (MTD line needs it); total_topups
    must only sum days inside [start_date, end_date]. A single query feeds both -- narrowing the
    fetch or widening the sum would silently break one of the two."""
    before_range = date(2099, 9, 5)  # same month, before start_date: in topups_daily, NOT in total
    in_range = date(2099, 9, 10)
    start_date, end_date = date(2099, 9, 8), date(2099, 9, 10)
    txn_ids: list[uuid.UUID] = []
    user_ids: list[uuid.UUID] = []
    async with AsyncSessionLocal() as db:
        user = User(email=f"revenue-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.flush()
        user_ids.append(user.id)

        for day, amount in ((before_range, 15.0), (in_range, 25.0)):
            txn = CreditTransaction(
                user_id=user.id, amount=amount, amount_left=amount,
                provider=CreditTransactionProvider.revolut, status=CreditTransactionStatus.completed,
            )
            txn.created_at = datetime.combine(day, datetime.min.time().replace(hour=12))
            db.add(txn)
            await db.flush()
            txn_ids.append(txn.id)

        headers = await _mk_staff_headers(db)
        await db.commit()

    try:
        resp = await async_client.get(
            f"/stats/global/subscriptions/revenue?start_date={start_date}&end_date={end_date}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        amounts_by_date = {d["date"]: d["amount"] for d in body["topups_daily"]}
        assert amounts_by_date.get(before_range.strftime("%Y-%m-%d")) == pytest.approx(15.0)
        assert amounts_by_date.get(in_range.strftime("%Y-%m-%d")) == pytest.approx(25.0)
        assert body["total_topups"] == pytest.approx(25.0)  # excludes the pre-range day
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(CreditTransaction).where(CreditTransaction.id.in_(txn_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()
