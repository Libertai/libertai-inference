from datetime import datetime

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import SessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class CreditService:
    @staticmethod
    def add_credits(
        provider: CreditTransactionProvider,
        address: str,
        amount: float,
        transaction_hash: str | None = None,
        block_number: int | None = None,
        expired_at: datetime | None = None,
        status: CreditTransactionStatus = CreditTransactionStatus.completed,
    ) -> bool:
        """
        Add credits to a user, creating the user if they don't exist.

        Args:
            provider: The provider used in the transaction
            address: User's blockchain address
            amount: USD amount to add
            transaction_hash: Transaction hash for recording the transaction (optional for vouchers)
            block_number: The block number this transaction was processed in
            expired_at: Optional expiration date for the credits
            status: Status of the transaction (pending or completed)

        Returns:
           Boolean indicating if the operation was successful
        """

        # Apply the boost for LTAI payments
        amount = amount * 100 / 80 if provider == CreditTransactionProvider.libertai else amount

        log_msg = f"Adding {amount} credits to address {address} with status {status.value}"
        if transaction_hash:
            log_msg += f" from tx {transaction_hash}"
        if block_number:
            log_msg += f" in block {block_number}"
        logger.debug(log_msg)

        try:
            with SessionLocal() as db:
                # Get or create user
                user = db.query(User).filter(User.address == address).first()
                if not user:
                    user = User(address=address)
                    db.add(user)
                    db.flush()  # Generate primary key

                # Check if transaction already exists (if a hash was provided)
                if transaction_hash:
                    existing_transaction = (
                        db.query(CreditTransaction)
                        .filter(CreditTransaction.transaction_hash == transaction_hash)
                        .first()
                    )
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
                db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding credits to {address}: {str(e)}", exc_info=True)
            raise e

    @staticmethod
    def use_credits(address: str, amount: float):
        """
        Deduce credits from the user's balance.

        Args:
            address: User's blockchain address
            amount: USD amount to use

        Returns:
            Boolean indicating if the operation was successful
        """
        logger.debug(f"Using {amount} credits from address {address}")

        try:
            # Get active transactions in order of expiration date (oldest first)
            with SessionLocal() as db:
                transactions: list[CreditTransaction] = (
                    db.query(CreditTransaction)
                    .filter(
                        CreditTransaction.address == address,
                        CreditTransaction.is_active == True,  # noqa: E712
                        CreditTransaction.status == CreditTransactionStatus.completed,
                    )
                    .order_by(
                        CreditTransaction.expired_at.asc().nullslast()  # Transactions with expiration dates first
                    )
                    .all()
                )

                remaining_amount = amount
                for tx in transactions:
                    available = tx.amount_left
                    if available <= 0:
                        continue

                    # Use as much as possible from this transaction
                    use_from_tx = min(available, remaining_amount)
                    tx.amount_left -= use_from_tx
                    remaining_amount -= use_from_tx

                    # If fully used, break
                    if remaining_amount <= 0:
                        break

                # Couldn't use all requested credits
                if remaining_amount > 0:
                    # Just issuing a warning, the request already took place so we can't really do anything
                    logger.warning(
                        f"Insufficient credits for {address}: requested {amount}, missing {remaining_amount}"
                    )

                db.commit()
                return True
        except Exception as e:
            logger.error(f"Error using credits from {address}: {str(e)}", exc_info=True)
            raise e

    @staticmethod
    def get_balance(address: str) -> float:
        """
        Get the current balance for a user.

        Args:
            address: User's blockchain address

        Returns:
            Current balance in USD
        """

        try:
            with SessionLocal() as db:
                # Get credit balance
                user = db.query(User).filter(User.address == address).first()
                if not user:
                    return 0

                balance = user.credit_balance
                return balance
        except Exception as e:
            logger.error(f"Error getting balance for {address}: {str(e)}", exc_info=True)
            return 0

    @staticmethod
    def get_vouchers(address: str) -> list[CreditTransaction]:
        """
        Get all voucher transactions for a user.

        Args:
            address: User's blockchain address

        Returns:
            List of voucher credit transactions
        """

        try:
            with SessionLocal() as db:
                vouchers = (
                    db.query(CreditTransaction)
                    .filter(
                        CreditTransaction.address == address,
                        CreditTransaction.provider == CreditTransactionProvider.voucher,
                    )
                    .order_by(CreditTransaction.created_at.desc())
                    .all()
                )
                return vouchers

        except Exception as e:
            logger.error(f"Error getting vouchers for {address}: {str(e)}", exc_info=True)
            return []

    @staticmethod
    def change_voucher_expiration_date(voucher_id: str, new_expiration: datetime | None) -> bool:
        """
        Change the expiration date of a voucher.

        Args:
            voucher_id: UUID of the voucher transaction
            new_expiration: New expiration date
        Returns:
            Boolean indicating if the operation was successful
        """

        try:
            with SessionLocal() as db:
                voucher = (
                    db.query(CreditTransaction)
                    .filter(
                        CreditTransaction.is_active == True,  # noqa: E712
                        CreditTransaction.id == voucher_id,
                        CreditTransaction.provider == CreditTransactionProvider.voucher,
                    )
                    .first()
                )

                if not voucher:
                    logger.warning(f"Voucher with ID {voucher_id} not found or not a voucher or already expired")
                    return False

                voucher.expired_at = new_expiration
                db.commit()
                return True

        except Exception as e:
            logger.error(f"Error expiring voucher {voucher_id}: {str(e)}", exc_info=True)
            return False

    @staticmethod
    def update_transaction_status(transaction_hash: str, status: CreditTransactionStatus) -> bool:
        """
        Update the status of a transaction.

        Args:
            transaction_hash: The transaction hash
            status: The new status for the transaction

        Returns:
            Boolean indicating if the operation was successful
        """
        try:
            with SessionLocal() as db:
                transaction = (
                    db.query(CreditTransaction).filter(CreditTransaction.transaction_hash == transaction_hash).first()
                )

                if not transaction:
                    logger.warning(f"Transaction with hash {transaction_hash} not found")
                    return False

                transaction.status = status
                db.commit()
                logger.info(f"Updated transaction {transaction_hash} status to {status.value}")
                return True

        except Exception as e:
            logger.error(f"Error updating transaction status for {transaction_hash}: {str(e)}", exc_info=True)
            return False
