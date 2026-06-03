import hashlib
from datetime import datetime, timedelta

from cryptography.fernet import Fernet
from eth_account import Account
from eth_account.messages import encode_defunct

from src.config import config
from src.models.auth_code import AuthCode
from src.models.base import AsyncSessionLocal
from src.services.magic_link import create_magic_link
from src.services.users import get_or_create_user_by_email


def _sign(account, message: str) -> str:
    signed = Account.sign_message(encode_defunct(text=message), account.key)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else f"0x{sig}"


async def test_wallet_login_flow_and_protected_route(async_client):
    account = Account.create()

    challenge = await async_client.post("/auth/wallet/challenge", json={"address": account.address})
    assert challenge.status_code == 200
    message = challenge.json()["message"]

    verify = await async_client.post(
        "/auth/wallet/verify", json={"address": account.address, "signature": _sign(account, message)}
    )
    assert verify.status_code == 200
    tokens = verify.json()
    assert tokens["access_token"] and tokens["refresh_token"]

    # The access token works on a protected endpoint.
    keys = await async_client.get("/api-keys", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert keys.status_code == 200


async def test_refresh_rotation(async_client):
    account = Account.create()
    message = (await async_client.post("/auth/wallet/challenge", json={"address": account.address})).json()["message"]
    pair = (
        await async_client.post(
            "/auth/wallet/verify", json={"address": account.address, "signature": _sign(account, message)}
        )
    ).json()

    rotated = await async_client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert rotated.status_code == 200

    # The old refresh token is now rejected.
    reused = await async_client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert reused.status_code == 401


async def test_email_magic_link_verify(async_client, monkeypatch):
    monkeypatch.setattr(config, "MAGIC_LINK_SECRET", "test-secret")
    async with AsyncSessionLocal() as db:
        _token, code = await create_magic_link(db, "ml-route@example.com")
        await db.commit()

    resp = await async_client.post(
        "/auth/verify-magic-link", json={"email": "ml-route@example.com", "code": code}
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


async def test_me_returns_user_profile(async_client):
    from src.services.auth_tokens import create_access_token

    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, "me-route@example.com")
        await db.commit()
        token = create_access_token(user.id)

    resp = await async_client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me-route@example.com"

    # Unauthenticated -> 401.
    assert (await async_client.get("/auth/me")).status_code == 401


async def test_oauth_exchange_one_time_code(async_client, monkeypatch):
    monkeypatch.setattr(config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "ENCRYPTION_KEY_PREVIOUS", None)
    from src.utils.encryption import encrypt

    code = "one-time-code-xyz"
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, "exch-route@example.com")
        db.add(
            AuthCode(
                code_hash=hashlib.sha256(code.encode()).hexdigest(),
                user_id=user.id,
                access_token=encrypt("access-abc"),
                refresh_token=encrypt("refresh-abc"),
                expires_at=datetime.now() + timedelta(seconds=60),
            )
        )
        await db.commit()

    resp = await async_client.post("/auth/exchange", json={"code": code})
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "access-abc"

    # Single-use: the code is gone.
    reused = await async_client.post("/auth/exchange", json={"code": code})
    assert reused.status_code == 400
