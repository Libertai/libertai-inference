"""Billing-details route tests (GET/PUT the 1:1 optional buyer identity)."""

import time

from sqlalchemy import delete

from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.models.user_billing_details import UserBillingDetails
from src.services.auth_tokens import create_access_token


async def _auth_user() -> tuple[User, dict]:
    async with AsyncSessionLocal() as db:
        user = User(email=f"billing-route-{int(time.time() * 1000)}@example.com")
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(UserBillingDetails).where(UserBillingDetails.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_get_empty_billing_details(async_client):
    user, headers = await _auth_user()
    try:
        resp = await async_client.get("/invoices/billing-details", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "name": None,
            "address_line1": None,
            "address_line2": None,
            "postal_code": None,
            "city": None,
            "country": None,
            "vat_number": None,
        }
    finally:
        await _cleanup(user.id)


async def test_put_then_get_roundtrip(async_client):
    user, headers = await _auth_user()
    try:
        put_resp = await async_client.put(
            "/invoices/billing-details", headers=headers, json={"name": "ACME", "vat_number": "FR123"}
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["name"] == "ACME"
        assert put_resp.json()["vat_number"] == "FR123"

        get_resp = await async_client.get("/invoices/billing-details", headers=headers)
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["name"] == "ACME"
        assert body["vat_number"] == "FR123"
    finally:
        await _cleanup(user.id)


async def test_control_chars_stripped_and_lengths_enforced(async_client):
    user, headers = await _auth_user()
    try:
        resp = await async_client.put("/invoices/billing-details", headers=headers, json={"name": "AC\x00ME\x1b"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "ACME"

        too_long_resp = await async_client.put("/invoices/billing-details", headers=headers, json={"name": "a" * 201})
        assert too_long_resp.status_code == 422
    finally:
        await _cleanup(user.id)


async def test_unauthenticated_rejected(async_client):
    get_resp = await async_client.get("/invoices/billing-details")
    assert get_resp.status_code == 401

    put_resp = await async_client.put("/invoices/billing-details", json={"name": "ACME"})
    assert put_resp.status_code == 401
