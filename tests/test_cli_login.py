"""CLI browser-SSO login: PKCE-bound one-time code + rotate-in-place CLI API key."""

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from src.config import config
from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.auth_tokens import create_access_token
from src.services.users import get_or_create_user_by_email

pytestmark = pytest.mark.asyncio


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# --- Route flow: mint code (authed) -> exchange with PKCE verifier ---


async def test_cli_code_exchange_with_pkce(async_client, monkeypatch):
    monkeypatch.setattr(config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "ENCRYPTION_KEY_PREVIOUS", None)
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
        token = create_access_token(user.id)
    try:
        verifier, challenge = _pkce()

        minted = await async_client.post(
            "/auth/cli/code", json={"challenge": challenge}, headers={"Authorization": f"Bearer {token}"}
        )
        assert minted.status_code == 200
        code = minted.json()["code"]

        exchanged = await async_client.post("/auth/exchange", json={"code": code, "verifier": verifier})
        assert exchanged.status_code == 200
        assert exchanged.json()["access_token"]

        # Single-use: the code is gone.
        assert (await async_client.post("/auth/exchange", json={"code": code, "verifier": verifier})).status_code == 400
    finally:
        await _cleanup(user.id)


async def test_cli_code_requires_auth(async_client):
    _, challenge = _pkce()
    assert (await async_client.post("/auth/cli/code", json={"challenge": challenge})).status_code == 401


async def test_exchange_rejects_wrong_or_missing_verifier(async_client, monkeypatch):
    monkeypatch.setattr(config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "ENCRYPTION_KEY_PREVIOUS", None)
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
        token = create_access_token(user.id)
    try:
        _, challenge = _pkce()

        # Wrong verifier -> 400.
        code = (
            await async_client.post(
                "/auth/cli/code", json={"challenge": challenge}, headers={"Authorization": f"Bearer {token}"}
            )
        ).json()["code"]
        assert (
            await async_client.post("/auth/exchange", json={"code": code, "verifier": "not-the-verifier"})
        ).status_code == 400

        # Missing verifier on a PKCE-bound code -> 400.
        code2 = (
            await async_client.post(
                "/auth/cli/code", json={"challenge": challenge}, headers={"Authorization": f"Bearer {token}"}
            )
        ).json()["code"]
        assert (await async_client.post("/auth/exchange", json={"code": code2})).status_code == 400
    finally:
        await _cleanup(user.id)


# --- Service: rotate-in-place + expiry gating ---


async def test_cli_key_rotates_in_place(async_client):
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
    try:
        first = await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="box")
        second = await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="box")

        assert first.id == second.id  # same DB row preserved (usage history intact)
        assert first.full_key != second.full_key  # secret rotated
        assert second.type == ApiKeyType.cli
        assert second.expires_at is not None and second.expires_at > datetime.now()

        # Only one CLI key for this device.
        listed = await ApiKeyService.get_cli_api_keys(user.id)
        assert len([k for k in listed if k.name == "libertai-cli@box"]) == 1
    finally:
        await _cleanup(user.id)


async def test_expired_cli_key_excluded_from_gateway(async_client):
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.flush()
        # Prepaid balance so the key is otherwise eligible.
        db.add(
            CreditTransaction(
                user_id=user.id, amount=5.0, amount_left=5.0,
                provider=CreditTransactionProvider.revolut, status=CreditTransactionStatus.completed,
            )
        )
        await db.commit()
        uid = user.id
    try:
        created = await ApiKeyService.rotate_or_create_cli_api_key(uid, host="box")
        # Live CLI key is on the gateway whitelist.
        assert created.full_key in (await ApiKeyService.get_admin_all_api_keys()).valid

        # Force-expire it -> drops off the whitelist.
        async with AsyncSessionLocal() as db:
            row = await db.get(ApiKeyDB, created.id)
            row.expires_at = datetime.now() - timedelta(seconds=1)
            await db.commit()
        assert created.full_key not in (await ApiKeyService.get_admin_all_api_keys()).valid
    finally:
        await _cleanup(uid)


async def test_duplicate_live_cli_name_rejected():
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                ApiKeyDB(
                    key=ApiKeyDB.generate_key(), name="libertai-cli@dup", user_id=user.id, type=ApiKeyType.cli
                )
            )
            await db.commit()

        with pytest.raises(IntegrityError):
            async with AsyncSessionLocal() as db:
                db.add(
                    ApiKeyDB(
                        key=ApiKeyDB.generate_key(), name="libertai-cli@dup", user_id=user.id, type=ApiKeyType.cli
                    )
                )
                await db.commit()
    finally:
        await _cleanup(user.id)


async def test_cli_key_reminted_after_disconnect():
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
    try:
        first = await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="box")
        await ApiKeyService.delete_api_key(first.id)

        second = await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="box")

        assert second.id != first.id  # soft-deleted row is outside the index, so a new row
        async with AsyncSessionLocal() as db:
            live = (
                (
                    await db.execute(
                        select(ApiKeyDB).where(
                            ApiKeyDB.user_id == user.id,
                            ApiKeyDB.name == "libertai-cli@box",
                            ApiKeyDB.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(live) == 1
    finally:
        await _cleanup(user.id)


async def test_cli_key_rotation_survives_repeated_calls():
    """The ON CONFLICT arbiter must be inferable on a generic plan, not just a custom one."""
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
    try:
        results = [await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="box") for _ in range(20)]
        assert len({r.id for r in results}) == 1
        assert len({r.full_key for r in results}) == 20
    finally:
        await _cleanup(user.id)


async def test_cli_key_list_reports_last_used():
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
    try:
        key = await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="box")

        listed = await ApiKeyService.get_cli_api_keys(user.id)
        assert len(listed) == 1
        assert listed[0].last_used_at is None

        async with AsyncSessionLocal() as db:
            for when in (datetime(2026, 1, 1, 9, 0), datetime(2026, 1, 2, 9, 0)):
                call = InferenceCall(api_key_id=key.id, credits_used=0.0, model_name="m", input_tokens=0, output_tokens=0)
                call.used_at = when
                db.add(call)
            await db.commit()

        listed = await ApiKeyService.get_cli_api_keys(user.id)
        assert listed[0].last_used_at == datetime(2026, 1, 2, 9, 0)
    finally:
        await _cleanup(user.id)


async def test_cli_key_list_omits_expired_and_disabled():
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
    try:
        expired = await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="old")
        disabled = await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="off")
        await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="live")

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ApiKeyDB).where(ApiKeyDB.id == expired.id).values(expires_at=datetime.now() - timedelta(days=1))
            )
            await db.execute(update(ApiKeyDB).where(ApiKeyDB.id == disabled.id).values(is_active=False))
            await db.commit()

        names = {k.name for k in await ApiKeyService.get_cli_api_keys(user.id)}
        assert names == {"libertai-cli@live"}
    finally:
        await _cleanup(user.id)


async def test_cli_key_list_treats_null_expiry_as_valid():
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, f"cli-{uuid.uuid4().hex}@example.com")
        await db.commit()
    try:
        key = await ApiKeyService.rotate_or_create_cli_api_key(user.id, host="forever")
        async with AsyncSessionLocal() as db:
            await db.execute(update(ApiKeyDB).where(ApiKeyDB.id == key.id).values(expires_at=None))
            await db.commit()

        assert len(await ApiKeyService.get_cli_api_keys(user.id)) == 1
    finally:
        await _cleanup(user.id)
