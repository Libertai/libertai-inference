"""Subscription/credits analytics stats endpoints.

The new /stats/global endpoints aggregate over the shared global tables, so each test seeds its
rows on a unique far-future date and queries exactly that day to stay isolated from other data.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import delete

from src.config import config
from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.models.user import User
from src.services.auth_tokens import create_access_token

pytestmark = pytest.mark.asyncio

_DAY = "2099-06-15"
_TS = datetime(2099, 6, 15, 12, 0, 0)


async def _mk_user(db) -> uuid.UUID:
    u = User(email=f"stats-{uuid.uuid4().hex}@example.com", email_verified=True)
    db.add(u)
    await db.flush()
    return u.id


async def _mk_staff_headers(db) -> tuple[dict, uuid.UUID]:
    u = User(email=f"stats-staff-{uuid.uuid4().hex}@example.com", email_verified=True)
    u.is_libertai_staff = True
    db.add(u)
    await db.flush()
    return {"Authorization": f"Bearer {create_access_token(u.id)}"}, u.id


async def _mk_key(db, *, user_id, key=None, type_=ApiKeyType.chat) -> uuid.UUID:
    k = ApiKey(key=key or ApiKey.generate_key(), name="stats", user_id=user_id, type=type_)
    db.add(k)
    await db.flush()
    return k.id


async def test_messages_by_segment_splits_anon_free_paid(async_client, monkeypatch):
    shared_key = f"shared-{uuid.uuid4().hex}"
    monkeypatch.setattr(config, "LIBERTAI_CHAT_API_KEY", shared_key)

    user_ids: list[uuid.UUID] = []
    async with AsyncSessionLocal() as db:
        anon_key = await _mk_key(db, user_id=None, key=shared_key)

        free_user = await _mk_user(db)
        free_key = await _mk_key(db, user_id=free_user)

        paid_user = await _mk_user(db)
        paid_key = await _mk_key(db, user_id=paid_user)
        db.add(PlanSubscription(user_id=paid_user, tier="plus", provider="revolut", status="active"))
        user_ids += [free_user, paid_user]

        headers, staff_id = await _mk_staff_headers(db)
        user_ids.append(staff_id)

        for key_id, n in ((anon_key, 3), (free_key, 2), (paid_key, 4)):
            for _ in range(n):
                cr = ChatRequest(api_key_id=key_id, input_tokens=1, output_tokens=1, cached_tokens=0, model_name="m")
                cr.created_at = _TS
                db.add(cr)
        await db.commit()

    try:
        resp = await async_client.get(
            f"/stats/global/messages-by-segment?start_date={_DAY}&end_date={_DAY}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        counts = {m["segment"]: m["message_count"] for m in body["messages"]}
        assert counts == {"anonymous": 3, "free": 2, "plus": 4}
        assert body["total_messages"] == 9
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ChatRequest).where(ChatRequest.created_at == _TS))
            await db.execute(delete(ApiKey).where(ApiKey.key == shared_key))
            await db.execute(delete(PlanSubscription).where(PlanSubscription.user_id.in_(user_ids)))
            await db.execute(delete(ApiKey).where(ApiKey.user_id.in_(user_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()


async def test_credits_consumption_splits_tier_and_prepaid(async_client):
    user_ids: list[uuid.UUID] = []
    async with AsyncSessionLocal() as db:
        user = await _mk_user(db)
        user_ids.append(user)
        key_id = await _mk_key(db, user_id=user, type_=ApiKeyType.api)
        # (credits_used, tier_credits_used) -> prepaid = credits - tier
        for credits, tier in ((1.0, 0.5), (0.3, 0.3)):
            ic = InferenceCall(
                api_key_id=key_id, credits_used=credits, tier_credits_used=tier,
                input_tokens=1, output_tokens=1, model_name="m",
            )
            ic.used_at = _TS
            db.add(ic)

        headers, staff_id = await _mk_staff_headers(db)
        user_ids.append(staff_id)
        await db.commit()

    try:
        resp = await async_client.get(
            f"/stats/global/credits-consumption?start_date={_DAY}&end_date={_DAY}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_tier_credits"] == pytest.approx(0.8)
        assert body["total_prepaid_credits"] == pytest.approx(0.5)
        assert body["total_credits"] == pytest.approx(1.3)
        assert len(body["daily"]) == 1
        assert body["daily"][0]["date"] == _DAY
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(InferenceCall).where(InferenceCall.used_at == _TS))
            await db.execute(delete(ApiKey).where(ApiKey.user_id.in_(user_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()


async def test_subscribers_over_time_dedups_resubscribe_and_skips_abandoned(async_client):
    """A same-day re-subscribe (churned span + fresh span) counts once; an abandoned checkout
    (no current_period_start) is never counted."""
    day = "2099-07-20"
    ts = datetime(2099, 7, 20, 12, 0, 0)
    user_ids: list[uuid.UUID] = []
    async with AsyncSessionLocal() as db:
        # User who churned then re-subscribed the same day: two plus rows covering `day`.
        resub = await _mk_user(db)
        churned = PlanSubscription(
            user_id=resub, tier="plus", provider="revolut", status="expired", current_period_start=ts
        )
        churned.updated_at = ts
        db.add(churned)
        db.add(PlanSubscription(user_id=resub, tier="plus", provider="revolut", status="active", current_period_start=ts))

        # User who abandoned checkout: expired, never reached a paid period.
        abandoned = await _mk_user(db)
        db.add(PlanSubscription(user_id=abandoned, tier="plus", provider="revolut", status="expired"))
        user_ids += [resub, abandoned]

        headers, staff_id = await _mk_staff_headers(db)
        user_ids.append(staff_id)
        await db.commit()

    try:
        resp = await async_client.get(
            f"/stats/global/subscribers-over-time?start_date={day}&end_date={day}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        plus = {d["date"]: d["active_subscribers"] for d in resp.json()["daily"] if d["tier"] == "plus"}
        # Only the re-subscriber counts, and only once despite two rows; abandoned is excluded.
        assert plus.get(day) == 1
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(PlanSubscription).where(PlanSubscription.user_id.in_(user_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()


async def test_subscriptions_snapshot_counts_segments(async_client):
    from src.models.anon_chat_usage import AnonChatUsage

    user_ids: list[uuid.UUID] = []
    anon_ip = f"198.51.100.{uuid.uuid4().int % 250}"
    async with AsyncSessionLocal() as db:
        paid = await _mk_user(db)
        db.add(PlanSubscription(user_id=paid, tier="max", provider="revolut", status="active"))
        free = await _mk_user(db)  # registered, no paid sub
        user_ids += [paid, free]
        db.add(AnonChatUsage(ip=anon_ip, window_started_at=_TS, count=3))

        headers, staff_id = await _mk_staff_headers(db)
        user_ids.append(staff_id)
        await db.commit()

    try:
        resp = await async_client.get("/stats/global/subscriptions", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        by_tier = {t["tier"]: t["active_subscribers"] for t in body["subscribers_by_tier"]}
        # Shared global tables, so assert our seeded rows are reflected (>=), not exact totals.
        assert by_tier.get("max", 0) >= 1
        assert body["total_paid_subscribers"] >= 1
        assert body["free_users"] >= 1
        assert body["anonymous_users"] >= 1
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(AnonChatUsage).where(AnonChatUsage.ip == anon_ip))
            await db.execute(delete(PlanSubscription).where(PlanSubscription.user_id.in_(user_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()
