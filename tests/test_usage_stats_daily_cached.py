import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import delete

from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.stats import StatsService


@pytest.mark.asyncio
async def test_daily_usage_reports_cached_tokens_as_a_subset_of_input():
    """The usage chart stacks cached under the rest of the input bar, so cached must arrive
    per day and must stay a subset of that day's input_tokens -- adding the two would
    otherwise overstate a heavily cached day."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    call_ids: list[int] = []

    async with AsyncSessionLocal() as db:
        user = User(email=f"usage-cached-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.flush()

        key = ApiKey(key=ApiKey.generate_key(), name="usage-cached", user_id=user.id)
        db.add(key)
        await db.flush()

        for day, input_tokens, cached_tokens in ((yesterday, 1000, 400), (today, 500, 0)):
            call = InferenceCall(
                api_key_id=key.id,
                credits_used=0.1,
                model_name="test-model",
                input_tokens=input_tokens,
                output_tokens=200,
                cached_tokens=cached_tokens,
            )
            call.used_at = datetime.combine(day, datetime.min.time().replace(hour=12))
            db.add(call)
            await db.flush()
            call_ids.append(call.id)

        await db.commit()
        user_id = user.id

    try:
        stats = await StatsService.get_usage_stats(user_id, yesterday, today)

        assert stats.daily_usage[yesterday.strftime("%Y-%m-%d")].cached_tokens == 400
        assert stats.daily_usage[yesterday.strftime("%Y-%m-%d")].input_tokens == 1000
        assert stats.daily_usage[today.strftime("%Y-%m-%d")].cached_tokens == 0
        assert stats.input_tokens == 1500
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(InferenceCall).where(InferenceCall.id.in_(call_ids)))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


@pytest.mark.asyncio
async def test_daily_usage_pads_days_without_calls_with_zero_cached():
    """Every day in the range is emitted, so the chart has no gaps -- a padded day needs a
    cached count too or the model rejects it."""
    today = date.today()
    start = today - timedelta(days=2)

    async with AsyncSessionLocal() as db:
        user = User(email=f"usage-empty-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.flush()
        db.add(ApiKey(key=ApiKey.generate_key(), name="usage-empty", user_id=user.id))
        await db.commit()
        user_id = user.id

    try:
        stats = await StatsService.get_usage_stats(user_id, start, today)

        assert len(stats.daily_usage) == 3
        assert all(day.cached_tokens == 0 for day in stats.daily_usage.values())
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
