"""Voucher route can credit any account: a wallet (chain+address) or an email-based user.

Exercises the real handler against the committed test DB (services open their own
sessions), so each test cleans up its own rows.
"""

import uuid

import pytest
from pydantic import ValidationError

from src.interfaces.credits import VoucherAddCreditsRequest
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
from src.routes.credits.voucher import add_voucher_credits
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_wallet

pytestmark = pytest.mark.asyncio

# A valid lowercase Base/EVM address (lowercase => no checksum requirement).
ADDR = "0x000000000000000000000000000000000000dead"


async def _balance(user_id) -> float:
    return await CreditService.get_balance(user_id)


async def _user_by_email(email: str) -> User | None:
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.email == email.strip().lower()))).scalars().first()


async def _cleanup_user(user_id):
    from sqlalchemy import delete

    from src.models.wallet_connection import WalletConnection

    async with AsyncSessionLocal() as db:
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(WalletConnection).where(WalletConnection.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# --- wallet path (unchanged behaviour) ---


async def test_wallet_path_credits_wallet_user():
    ok = await add_voucher_credits(VoucherAddCreditsRequest(chain="base", address=ADDR, amount=7.0))
    assert ok is True
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, ADDR)
        await db.commit()
    try:
        assert await _balance(user.id) == pytest.approx(7.0)
    finally:
        await _cleanup_user(user.id)


# --- email path (new) ---


async def test_email_unknown_account_is_created_and_credited():
    email = f"voucher-{uuid.uuid4().hex}@example.com"
    ok = await add_voucher_credits(VoucherAddCreditsRequest(email=email, amount=12.0))
    assert ok is True
    user = await _user_by_email(email)
    assert user is not None  # account auto-created
    try:
        assert await _balance(user.id) == pytest.approx(12.0)
    finally:
        await _cleanup_user(user.id)


async def test_email_path_credits_existing_user():
    email = f"voucher-{uuid.uuid4().hex}@example.com"
    async with AsyncSessionLocal() as db:
        user = User(email=email)
        db.add(user)
        await db.commit()
        user_id = user.id
    try:
        ok = await add_voucher_credits(VoucherAddCreditsRequest(email=email.upper(), amount=3.0))
        assert ok is True
        # Same user (email normalised), not a duplicate.
        assert (await _user_by_email(email)).id == user_id
        assert await _balance(user_id) == pytest.approx(3.0)
    finally:
        await _cleanup_user(user_id)


# --- validation: exactly one recipient ---


async def test_rejects_both_wallet_and_email():
    with pytest.raises(ValidationError):
        VoucherAddCreditsRequest(chain="base", address=ADDR, email="a@b.com", amount=1.0)


async def test_rejects_neither_wallet_nor_email():
    with pytest.raises(ValidationError):
        VoucherAddCreditsRequest(amount=1.0)


async def test_rejects_address_without_chain():
    with pytest.raises(ValidationError):
        VoucherAddCreditsRequest(address=ADDR, amount=1.0)
