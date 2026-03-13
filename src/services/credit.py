from datetime import datetime

from sqlalchemy import select, func

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
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
                # Get or create user
                result = await db.execute(select(User).where(User.address == address))
                user = result.scalars().first()
                if not user:
                    user = User(address=address)
                    db.add(user)
                    await db.flush()

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
                    transaction_hash=transaction_hash,
                    address=address,
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
    async def use_credits(address: str, amount: float):
        logger.debug(f"Using {amount} credits from address {address}")

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(CreditTransaction)
                    .where(
                        CreditTransaction.address == address,
                        CreditTransaction.is_active == True,  # noqa: E712
                        CreditTransaction.status == CreditTransactionStatus.completed,
                    )
                    .order_by(CreditTransaction.expired_at.asc().nullslast())
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

                if remaining_amount > 0:
                    logger.warning(
                        f"Insufficient credits for {address}: requested {amount}, missing {remaining_amount}"
                    )

                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error using credits from {address}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def get_balance(address: str) -> float:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                        CreditTransaction.address == address,
                        CreditTransaction.is_active == True,  # noqa: E712
                        CreditTransaction.status == CreditTransactionStatus.completed,
                    )
                )
                balance = result.scalar()
                return float(balance or 0.0)
        except Exception as e:
            logger.error(f"Error getting balance for {address}: {str(e)}", exc_info=True)
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
