"""Credit + API-key services re-keyed onto user_id (Phase 1).

These call the real services (which use their own AsyncSessionLocal bound to the
test DB) and assert the address->user resolution + user_id wiring. Each test uses
a unique address so committed rows don't collide within the session.
"""

from datetime import datetime

from sqlalchemy import select

from src.interfaces.credits import CreditTransactionProvider
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
from src.models.wallet_connection import WalletConnection
from src.services.api_key import ApiKeyService
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_wallet


async def _user_for_address(address: str) -> User:
    async with AsyncSessionLocal() as db:
        wallet = (
            await db.execute(select(WalletConnection).where(WalletConnection.address == address))
        ).scalars().first()
        assert wallet is not None
        user = await db.get(User, wallet.user_id)
        assert user is not None
        return user


async def test_add_credits_creates_user_wallet_and_balance():
    address = "0xCaFe000000000000000000000000000000000001"
    assert await CreditService.add_credits(CreditTransactionProvider.thirdweb, address, 10.0)

    async with AsyncSessionLocal() as db:
        wallet = (
            await db.execute(select(WalletConnection).where(WalletConnection.address == address))
        ).scalars().first()
        assert wallet is not None
        assert wallet.chain == "base"
        assert wallet.is_primary is True

        tx = (
            await db.execute(select(CreditTransaction).where(CreditTransaction.user_id == wallet.user_id))
        ).scalars().first()
        assert tx is not None
        assert tx.address == address  # legacy column still populated

        user = await db.get(User, wallet.user_id)
        assert user is not None
        assert await user.get_credit_balance() == 10.0


async def test_add_credits_for_user_adds_to_same_balance():
    address = "0xBeeF000000000000000000000000000000000002"
    assert await CreditService.add_credits(CreditTransactionProvider.thirdweb, address, 10.0)
    user = await _user_for_address(address)

    assert await CreditService.add_credits_for_user(user.id, 5.0, CreditTransactionProvider.voucher)
    assert await user.get_credit_balance() == 15.0


async def test_use_credits_reports_full_vs_insufficient():
    address = "0xFee1000000000000000000000000000000000004"
    assert await CreditService.add_credits(CreditTransactionProvider.thirdweb, address, 3.0)
    user = await _user_for_address(address)

    # Full deduction within balance -> True.
    assert await CreditService.use_credits(user.id, 1.0) is True
    assert await user.get_credit_balance() == 2.0

    # Overdraft -> False and nothing deducted (all-or-nothing by default, so a
    # failed charge never eats the remaining balance).
    assert await CreditService.use_credits(user.id, 5.0) is False
    assert await user.get_credit_balance() == 2.0

    # Post-hoc billing opts into partial capture: drain what's there, report False.
    assert await CreditService.use_credits(user.id, 5.0, allow_partial=True) is False
    assert await user.get_credit_balance() == 0.0


async def test_concurrent_use_credits_serialize_no_lost_update():
    """Two concurrent deductions (own sessions, like inference overflow racing the
    renewal cron) must serialize on row locks: both apply, neither is lost."""
    import asyncio

    address = "0xFee1000000000000000000000000000000000044"
    assert await CreditService.add_credits(CreditTransactionProvider.thirdweb, address, 10.0)
    user = await _user_for_address(address)

    results = await asyncio.gather(
        CreditService.use_credits(user.id, 3.0),
        CreditService.use_credits(user.id, 3.0),
    )

    assert results == [True, True]
    # Pre-fix, a lost update could leave the balance at 7.0 (one deduction overwritten).
    assert await user.get_credit_balance() == 4.0


async def test_use_credits_drains_oldest_top_up_first():
    """Among non-expiring top-ups, the oldest (by created_at) must drain first,
    regardless of insertion order."""
    address = "0x0LdE000000000000000000000000000000000005"
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id

        # Insert the NEWER top-up first so physical/insert order != age order.
        newer = CreditTransaction(
            user_id=user_id, amount=10.0, amount_left=10.0, provider=CreditTransactionProvider.voucher
        )
        newer.created_at = datetime(2025, 1, 1)
        older = CreditTransaction(
            user_id=user_id, amount=10.0, amount_left=10.0, provider=CreditTransactionProvider.voucher
        )
        older.created_at = datetime(2024, 1, 1)
        db.add(newer)
        db.add(older)
        await db.commit()
        older_id, newer_id = older.id, newer.id

    # Deduct an amount only one top-up can cover -> must come from the older one.
    assert await CreditService.use_credits(user_id, 4.0) is True

    async with AsyncSessionLocal() as db:
        older_row = await db.get(CreditTransaction, older_id)
        newer_row = await db.get(CreditTransaction, newer_id)
        assert older_row.amount_left == 6.0  # oldest drained first
        assert newer_row.amount_left == 10.0  # newer untouched


async def test_use_credits_prefers_expiring_over_older_unexpiring():
    """A newer top-up that expires must drain before an older one with no expiry."""
    address = "0xExp1000000000000000000000000000000000006"
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id

        old_no_expiry = CreditTransaction(
            user_id=user_id, amount=10.0, amount_left=10.0, provider=CreditTransactionProvider.voucher
        )
        old_no_expiry.created_at = datetime(2024, 1, 1)
        newer_expiring = CreditTransaction(
            user_id=user_id,
            amount=10.0,
            amount_left=10.0,
            provider=CreditTransactionProvider.voucher,
            expired_at=datetime(2030, 1, 1),
        )
        newer_expiring.created_at = datetime(2025, 1, 1)
        db.add(old_no_expiry)
        db.add(newer_expiring)
        await db.commit()
        old_id, exp_id = old_no_expiry.id, newer_expiring.id

    assert await CreditService.use_credits(user_id, 4.0) is True

    async with AsyncSessionLocal() as db:
        old_row = await db.get(CreditTransaction, old_id)
        exp_row = await db.get(CreditTransaction, exp_id)
        assert exp_row.amount_left == 6.0  # expiring drained first
        assert old_row.amount_left == 10.0  # non-expiring untouched


async def test_create_api_key_sets_user_id():
    address = "0xD00D000000000000000000000000000000000003"
    async with AsyncSessionLocal() as db:
        user = await get_or_create_user_by_wallet(db, address)
        await db.commit()
        user_id = user.id

    full_key = await ApiKeyService.create_api_key(user_id=user_id, name="my key", user_address=address)

    async with AsyncSessionLocal() as db:
        api_key = (await db.execute(select(ApiKeyDB).where(ApiKeyDB.id == full_key.id))).scalars().first()
        assert api_key is not None
        assert api_key.user_id == user_id
        assert api_key.user_address == address
