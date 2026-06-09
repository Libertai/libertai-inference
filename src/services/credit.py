import uuid
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.services.users import get_or_create_user_by_wallet
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class CreditService:
    @staticmethod
    async def add_credits(
        provider: CreditTransactionProvider,
        address: str,
        amount: float,
        transaction_hash: str | None = None,
        block_number: int | None = None,
        expired_at: datetime | None = None,
        status: CreditTransactionStatus = CreditTransactionStatus.completed,
    ) -> bool:
        # Apply the boost for LTAI payments
        amount = (
            amount * 100 / 80
            if provider in [CreditTransactionProvider.ltai_base, CreditTransactionProvider.ltai_solana]
            else amount
        )

        log_msg = f"Adding {amount} credits to address {address} with status {status.value}"
        if transaction_hash:
            log_msg += f" from tx {transaction_hash}"
        if block_number:
            log_msg += f" in block {block_number}"
        logger.debug(log_msg)

        try:
            async with AsyncSessionLocal() as db:
                # Resolve the wallet address to a user (creating the user + wallet link if new).
                user = await get_or_create_user_by_wallet(db, address)

                # Check if transaction already exists (if a hash was provided)
                if transaction_hash:
                    result = await db.execute(
                        select(CreditTransaction).where(CreditTransaction.transaction_hash == transaction_hash)
                    )
                    existing_transaction = result.scalars().first()
                    if existing_transaction:
                        logger.warning(f"Transaction {transaction_hash} already processed, skipping")
                        return False

                # Record transaction
                transaction = CreditTransaction(
                    user_id=user.id,
                    address=address,
                    transaction_hash=transaction_hash,
                    amount=amount,
                    amount_left=amount,
                    provider=provider,
                    block_number=block_number,
                    expired_at=expired_at,
                    is_active=True,
                    status=status,
                )
                db.add(transaction)
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding credits to {address}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def add_credits_for_user(
        user_id: uuid.UUID,
        amount: float,
        provider: CreditTransactionProvider,
        transaction_hash: str | None = None,
        expired_at: datetime | None = None,
        status: CreditTransactionStatus = CreditTransactionStatus.completed,
    ) -> bool:
        """Add credits to a user by id (fiat top-ups, trials) — no wallet involved.

        If ``transaction_hash`` is given it is used for idempotency: a replayed
        webhook with the same hash is skipped (returns ``False``).
        """
        logger.debug(f"Adding {amount} credits to user {user_id} with status {status.value}")
        try:
            async with AsyncSessionLocal() as db:
                if transaction_hash:
                    existing = (
                        await db.execute(
                            select(CreditTransaction).where(
                                CreditTransaction.transaction_hash == transaction_hash
                            )
                        )
                    ).scalars().first()
                    if existing:
                        logger.warning(f"Transaction {transaction_hash} already processed, skipping")
                        return False

                transaction = CreditTransaction(
                    user_id=user_id,
                    amount=amount,
                    amount_left=amount,
                    provider=provider,
                    transaction_hash=transaction_hash,
                    expired_at=expired_at,
                    is_active=True,
                    status=status,
                )
                db.add(transaction)
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding credits to user {user_id}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def _use_credits_on_session(db: AsyncSession, user_id: uuid.UUID, amount: float) -> bool:
        result = await db.execute(
            select(CreditTransaction)
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.is_active == True,  # noqa: E712
                CreditTransaction.status == CreditTransactionStatus.completed,
            )
            .order_by(
                CreditTransaction.expired_at.asc().nullslast(),
                CreditTransaction.created_at.asc(),
            )
        )
        transactions = result.scalars().all()

        remaining_amount = amount
        for tx in transactions:
            available = tx.amount_left
            if available <= 0:
                continue

            use_from_tx = min(available, remaining_amount)
            tx.amount_left -= use_from_tx
            remaining_amount -= use_from_tx

            if remaining_amount <= 0:
                break

        fully_deducted = remaining_amount <= 0
        if not fully_deducted:
            logger.warning(
                f"Insufficient credits for user {user_id}: requested {amount}, missing {remaining_amount}"
            )
        return fully_deducted

    @staticmethod
    async def use_credits(user_id: uuid.UUID, amount: float, db: AsyncSession | None = None) -> bool:
        """Deduct credits from a user's active transactions (oldest-expiring first,
        then oldest top-up first among transactions that share an expiry / have none).

        Returns ``True`` if the full amount was deducted, ``False`` if the balance was
        insufficient (the available credits are still drained to 0 in that case).

        If ``db`` is provided the deduction runs on that session and is only flushed (the
        caller owns the commit), so it shares the caller's transaction. If ``db`` is None,
        a dedicated session is opened and committed (legacy behavior).
        """
        logger.debug(f"Using {amount} credits from user {user_id}")

        if db is not None:
            fully_deducted = await CreditService._use_credits_on_session(db, user_id, amount)
            await db.flush()
            return fully_deducted

        try:
            async with AsyncSessionLocal() as own_db:
                fully_deducted = await CreditService._use_credits_on_session(own_db, user_id, amount)
                await own_db.commit()
                return fully_deducted
        except Exception as e:
            logger.error(f"Error using credits from user {user_id}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def _get_balance_on_session(db: AsyncSession, user_id: uuid.UUID) -> float:
        result = await db.execute(
            select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.is_active == True,  # noqa: E712
                CreditTransaction.status == CreditTransactionStatus.completed,
            )
        )
        balance = result.scalar()
        return float(balance or 0.0)

    @staticmethod
    async def get_balance(user_id: uuid.UUID, db: AsyncSession | None = None) -> float:
        """Return the user's spendable credit balance.

        If ``db`` is provided the query runs on the caller's session (sees its
        uncommitted writes); otherwise a dedicated session is opened.
        """
        if db is not None:
            return await CreditService._get_balance_on_session(db, user_id)

        try:
            async with AsyncSessionLocal() as own_db:
                return await CreditService._get_balance_on_session(own_db, user_id)
        except Exception as e:
            logger.error(f"Error getting balance for user {user_id}: {str(e)}", exc_info=True)
            return 0

    @staticmethod
    async def get_vouchers(address: str) -> list[CreditTransaction]:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(CreditTransaction)
                    .where(
                        CreditTransaction.address == address,
                        CreditTransaction.provider == CreditTransactionProvider.voucher,
                    )
                    .order_by(CreditTransaction.created_at.desc())
                )
                return list(result.scalars().all())
        except Exception as e:
            logger.error(f"Error getting vouchers for {address}: {str(e)}", exc_info=True)
            return []

    @staticmethod
    async def change_voucher_expiration_date(voucher_id: str, new_expiration: datetime | None) -> bool:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(CreditTransaction).where(
                        CreditTransaction.is_active == True,  # noqa: E712
                        CreditTransaction.id == voucher_id,
                        CreditTransaction.provider == CreditTransactionProvider.voucher,
                    )
                )
                voucher = result.scalars().first()

                if not voucher:
                    logger.warning(f"Voucher with ID {voucher_id} not found or not a voucher or already expired")
                    return False

                voucher.expired_at = new_expiration
                await db.commit()
                return True

        except Exception as e:
            logger.error(f"Error expiring voucher {voucher_id}: {str(e)}", exc_info=True)
            return False

    @staticmethod
    async def update_transaction_status(transaction_hash: str, status: CreditTransactionStatus) -> bool:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(CreditTransaction).where(CreditTransaction.transaction_hash == transaction_hash)
                )
                transaction = result.scalars().first()

                if not transaction:
                    logger.warning(f"Transaction with hash {transaction_hash} not found")
                    return False

                transaction.status = status
                await db.commit()
                logger.info(f"Updated transaction {transaction_hash} status to {status.value}")
                return True

        except Exception as e:
            logger.error(f"Error updating transaction status for {transaction_hash}: {str(e)}", exc_info=True)
            return False
