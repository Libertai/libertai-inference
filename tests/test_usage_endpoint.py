"""Public ``GET /usage``: API-key auth and the percentage-only allowance payload.

Runs against the committed DB with real ``datetime.now()``; each test cleans up its rows.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.entitlement_window import EntitlementWindow
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.auth_tokens import create_access_token
from src.services.entitlement import WINDOW_5H, WINDOW_WEEKLY
from src.subscription_tiers import get_tier


async def _setup(
    *,
    key_type: ApiKeyType = ApiKeyType.api,
    is_active: bool = True,
    deleted: bool = False,
    expires_at: datetime | None = None,
    suspended: bool = False,
    cap: float | None = None,
    prepaid: float = 0.0,
    windows: bool = False,
    tier_usage: float = 0.0,
    overflow: float = 0.0,
) -> tuple[uuid.UUID, str]:
    """User + one API key, returning (user_id, raw key).

    ``windows`` opens both entitlement windows now; ``tier_usage`` seeds plan-covered
    usage inside them, ``overflow`` seeds prepaid-covered usage this month.
    """
    now = datetime.now()
    async with AsyncSessionLocal() as db:
        user = User(email=f"usage-{uuid.uuid4().hex}@example.com")
        user.monthly_extra_credit_cap = cap
        if suspended:
            user.suspended_at = now
        db.add(user)
        await db.flush()

        key = ApiKeyDB(
            key=ApiKeyDB.generate_key(),
            name=uuid.uuid4().hex,
            user_id=user.id,
            type=key_type,
            expires_at=expires_at,
        )
        key.is_active = is_active
        if deleted:
            key.deleted_at = now
        db.add(key)
        await db.flush()

        if windows:
            for kind in (WINDOW_5H, WINDOW_WEEKLY):
                db.add(
                    EntitlementWindow(
                        user_id=user.id,
                        kind=kind,
                        started_at=now - timedelta(hours=1),
                        expires_at=now + timedelta(hours=4),
                    )
                )
        if tier_usage:
            call = InferenceCall(
                api_key_id=key.id, credits_used=tier_usage, model_name="m", tier_credits_used=tier_usage
            )
            call.used_at = now - timedelta(minutes=30)
            db.add(call)
        if overflow:
            call = InferenceCall(api_key_id=key.id, credits_used=overflow, model_name="m", tier_credits_used=0.0)
            call.used_at = now - timedelta(minutes=20)
            db.add(call)
        if prepaid:
            db.add(
                CreditTransaction(
                    user_id=user.id,
                    amount=prepaid,
                    amount_left=prepaid,
                    provider=CreditTransactionProvider.revolut,
                    status=CreditTransactionStatus.completed,
                )
            )
        await db.commit()
        return user.id, key.key


async def _cleanup(user_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(EntitlementWindow).where(EntitlementWindow.user_id == user_id))
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_usage_requires_a_key(async_client):
    assert (await async_client.get("/usage")).status_code == 401


async def test_usage_rejects_unknown_key(async_client):
    resp = await async_client.get("/usage", headers=_bearer(ApiKeyDB.generate_key()))
    assert resp.status_code == 401


async def test_usage_rejects_session_token(async_client):
    """A console session JWT is not an API key — this surface is key-only."""
    user_id, _ = await _setup()
    try:
        resp = await async_client.get("/usage", headers=_bearer(create_access_token(user_id)))
        assert resp.status_code == 401
    finally:
        await _cleanup(user_id)


async def test_usage_accepts_api_key(async_client):
    user_id, key = await _setup()
    try:
        resp = await async_client.get("/usage", headers=_bearer(key))
        assert resp.status_code == 200
        assert resp.json()["plan"] == "free"
    finally:
        await _cleanup(user_id)


async def test_usage_accepts_cli_key(async_client):
    user_id, key = await _setup(key_type=ApiKeyType.cli, expires_at=datetime.now() + timedelta(days=30))
    try:
        assert (await async_client.get("/usage", headers=_bearer(key))).status_code == 200
    finally:
        await _cleanup(user_id)


async def test_usage_rejects_non_user_key_types(async_client):
    """chat / liberclaw / x402 / pool keys are service- or app-owned, not a user's own credentials."""
    for key_type in (ApiKeyType.chat, ApiKeyType.liberclaw, ApiKeyType.x402, ApiKeyType.pool):
        user_id, key = await _setup(key_type=key_type)
        try:
            resp = await async_client.get("/usage", headers=_bearer(key))
            assert resp.status_code == 401, key_type
        finally:
            await _cleanup(user_id)


async def test_usage_rejects_unusable_keys(async_client):
    """A key that can't run inference can't read usage either."""
    for kwargs in (
        {"is_active": False},
        {"deleted": True},
        {"expires_at": datetime.now() - timedelta(days=1)},
        {"suspended": True},
    ):
        user_id, key = await _setup(**kwargs)  # type: ignore[arg-type]
        try:
            resp = await async_client.get("/usage", headers=_bearer(key))
            assert resp.status_code == 401, kwargs
        finally:
            await _cleanup(user_id)


async def test_usage_reports_window_percentages(async_client):
    """Half the free tier's 5h allowance spent reads as 50%, and against the weekly limit too."""
    free = get_tier("free")
    spent = free.window_5h_credits / 2
    user_id, key = await _setup(windows=True, tier_usage=spent)
    try:
        body = (await async_client.get("/usage", headers=_bearer(key))).json()
        assert body["window_5h"]["used_percent"] == 50.0
        assert body["weekly"]["used_percent"] == round(spent / free.weekly_credits * 100, 2)
    finally:
        await _cleanup(user_id)


async def test_usage_percentage_clamped_at_100(async_client):
    """Overflow spend can push usage past the limit; the reported share stops at 100."""
    user_id, key = await _setup(windows=True, tier_usage=get_tier("free").weekly_credits * 3)
    try:
        body = (await async_client.get("/usage", headers=_bearer(key))).json()
        assert body["window_5h"]["used_percent"] == 100.0
        assert body["weekly"]["used_percent"] == 100.0
    finally:
        await _cleanup(user_id)


async def test_usage_without_open_window_reports_zero_and_no_reset(async_client):
    user_id, key = await _setup()
    try:
        body = (await async_client.get("/usage", headers=_bearer(key))).json()
        assert body["window_5h"] == {"used_percent": 0.0, "resets_at": None}
        assert body["weekly"] == {"used_percent": 0.0, "resets_at": None}
    finally:
        await _cleanup(user_id)


async def test_usage_resets_at_serialized_as_utc(async_client):
    """Naive UTC columns must serialize with an offset, or JS reads them as browser-local."""
    user_id, key = await _setup(windows=True)
    try:
        resets_at = (await async_client.get("/usage", headers=_bearer(key))).json()["window_5h"]["resets_at"]
        assert resets_at.endswith("+00:00")
        assert datetime.fromisoformat(resets_at).tzinfo is not None
    finally:
        await _cleanup(user_id)


async def test_usage_extra_credits_are_the_raw_balance_when_uncapped(async_client):
    user_id, key = await _setup(prepaid=25.0)
    try:
        body = (await async_client.get("/usage", headers=_bearer(key))).json()
        assert body["extra_usage_credits"] == 25.0
    finally:
        await _cleanup(user_id)


async def test_usage_extra_credits_respect_the_monthly_cap(async_client):
    """Balance is 50 but only 10/month may overflow, 4 of which is already spent."""
    user_id, key = await _setup(prepaid=50.0, cap=10.0, windows=True, overflow=4.0)
    try:
        body = (await async_client.get("/usage", headers=_bearer(key))).json()
        assert body["extra_usage_credits"] == 6.0
    finally:
        await _cleanup(user_id)


async def test_usage_never_exposes_raw_credit_amounts(async_client):
    """The point of the endpoint: shares the share, not the plan's credit allowances."""
    user_id, key = await _setup(windows=True, tier_usage=0.1)
    try:
        body = (await async_client.get("/usage", headers=_bearer(key))).json()
        assert set(body) == {"plan", "window_5h", "weekly", "extra_usage_credits"}
        assert set(body["window_5h"]) == {"used_percent", "resets_at"}
    finally:
        await _cleanup(user_id)
