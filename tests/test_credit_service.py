"""Credit + API-key services re-keyed onto user_id (Phase 1).

These call the real services (which use their own AsyncSessionLocal bound to the
test DB) and assert the address->user resolution + user_id wiring. Each test uses
a unique address so committed rows don't collide within the session.
"""

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
