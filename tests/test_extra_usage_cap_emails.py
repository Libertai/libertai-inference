"""75%/90% warnings for the monthly extra-usage credit cap.

The cron opens its own sessions, so data is committed and cleaned up per test."""

import uuid
from datetime import datetime

from sqlalchemy import delete, select

import src.services.lifecycle_email as lifecycle
from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.lifecycle_email_send import LifecycleEmailSend
from src.models.user import User
from src.routes.emails import check_extra_usage_caps


async def _setup(cap: float | None, overflow: float) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as db:
        user = User(email=f"cap-email-{uuid.uuid4().hex}@example.com")
        user.monthly_extra_credit_cap = cap
        db.add(user)
        await db.flush()
        key = ApiKeyDB(key=ApiKeyDB.generate_key(), name=uuid.uuid4().hex, user_id=user.id, type=ApiKeyType.api)
        db.add(key)
        await db.flush()
        if overflow:
            await _add_overflow(db, key.id, overflow)
        await db.commit()
        return user.id, key.id


async def _add_overflow(db, key_id, amount: float) -> None:
    call = InferenceCall(api_key_id=key_id, credits_used=amount, model_name="m", tier_credits_used=0.0)
    call.used_at = datetime.now()
    db.add(call)


async def _cleanup(*user_ids) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id.in_(user_ids)))
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.commit()


async def _send_types(user_id) -> list[str]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(LifecycleEmailSend.email_type)
                    .where(LifecycleEmailSend.user_id == user_id)
                    .order_by(LifecycleEmailSend.sent_at)
                )
            ).scalars()
        )


async def test_warns_at_75_then_90_once_per_month():
    user_id, key_id = await _setup(cap=10.0, overflow=8.0)  # 80%
    try:
        assert await check_extra_usage_caps() == 1
        assert await _send_types(user_id) == ["extra_usage_cap_75"]
        # Re-running the cron does not repeat the warning this month.
        assert await check_extra_usage_caps() == 0

        async with AsyncSessionLocal() as db:  # bump to 95%
            await _add_overflow(db, key_id, 1.5)
            await db.commit()
        assert await check_extra_usage_caps() == 1
        assert await _send_types(user_id) == ["extra_usage_cap_75", "extra_usage_cap_90"]
    finally:
        await _cleanup(user_id)


async def test_no_warning_without_cap_or_below_threshold():
    uncapped, _ = await _setup(cap=None, overflow=50.0)
    low_usage, _ = await _setup(cap=10.0, overflow=5.0)  # 50%
    try:
        assert await check_extra_usage_caps() == 0
        assert await _send_types(uncapped) == []
        assert await _send_types(low_usage) == []
    finally:
        await _cleanup(uncapped, low_usage)


async def test_opted_out_user_still_warned():
    """The warning is transactional: it reports on a limit the user set, and on usage pausing."""
    user_id, _ = await _setup(cap=10.0, overflow=8.0)
    async with AsyncSessionLocal() as db:
        (await db.get(User, user_id)).lifecycle_emails_opt_out = True
        await db.commit()
    try:
        assert await check_extra_usage_caps() == 1
        assert await _send_types(user_id) == ["extra_usage_cap_75"]
    finally:
        await _cleanup(user_id)


async def test_one_failure_does_not_resend_the_rest(monkeypatch):
    """A send that blows up must not roll back the log rows of mail already delivered."""
    good, _ = await _setup(cap=10.0, overflow=8.0)
    bad, _ = await _setup(cap=10.0, overflow=8.0)
    async with AsyncSessionLocal() as db:
        bad_email = (await db.get(User, bad)).email

    real_send = lifecycle.send_email

    async def flaky(to, *args, **kwargs):
        if to == bad_email:
            raise RuntimeError("transport exploded")
        return await real_send(to, *args, **kwargs)

    monkeypatch.setattr(lifecycle, "send_email", flaky)
    try:
        assert await check_extra_usage_caps() == 1
        assert await _send_types(good) == ["extra_usage_cap_75"]
        assert await _send_types(bad) == []

        # The sweep runs again 10 minutes later: the delivered warning is not repeated.
        monkeypatch.setattr(lifecycle, "send_email", real_send)
        assert await check_extra_usage_caps() == 1
        assert await _send_types(good) == ["extra_usage_cap_75"]
        assert await _send_types(bad) == ["extra_usage_cap_75"]
    finally:
        await _cleanup(good, bad)


async def test_no_warning_once_the_cap_is_reached():
    """Past the cap the keys are already blocked, so the "usage will pause" warning is wrong."""
    user_id, _ = await _setup(cap=10.0, overflow=10.4)  # 104%
    try:
        assert await check_extra_usage_caps() == 0
        assert await _send_types(user_id) == []
    finally:
        await _cleanup(user_id)


async def test_jumping_straight_past_90_sends_only_the_90_warning():
    user_id, _ = await _setup(cap=10.0, overflow=9.6)  # 96%
    try:
        assert await check_extra_usage_caps() == 1
        assert await _send_types(user_id) == ["extra_usage_cap_90"]
    finally:
        await _cleanup(user_id)
