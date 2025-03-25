from datetime import datetime

from src.interfaces.credits import CreditTransactionProvider
from src.models.base import SessionLocal
from src.models.credit_balance import CreditBalance
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
        transaction_hash: str,
        block_number: int | None = None,
        expired_at: datetime | None = None,
    ):
        """
        Add credits to a user, creating the user if they don't exist.

        Args:
            provider: The provider used in the transaction
            address: User's blockchain address
            amount: USD amount to add
            transaction_hash: Transaction hash for recording the transaction
            block_number: The block number this transaction was processed in
            expired_at: Optional expiration date for the credits

        Returns:
            Updated CreditBalance object
        """
        logger.debug(
            f"Adding {amount} credits to address {address} from tx {transaction_hash} in block {block_number}"
        )
        db = SessionLocal()

        try:
            # Get or create user
            user = db.query(User).filter(User.address == address).first()
            if not user:
                user = User(address=address)
                db.add(user)
                db.flush()  # Generate primary key

            # Get or create credit balance
            credit_balance = db.query(CreditBalance).filter(CreditBalance.address == address).first()
            if not credit_balance:
                credit_balance = CreditBalance(address=address)
                db.add(credit_balance)
                db.flush()  # Make sure the instance is persisted and has all attributes populated

            # Record transaction
            transaction = CreditTransaction(
                transaction_hash=transaction_hash,
                address=address,
                amount=amount,
                amount_left=amount,
                provider=provider.value,
                block_number=block_number,
                expired_at=expired_at,
                is_active=True,
            )
            db.add(transaction)

            db.commit()

            # To make the session available when calculating balance
            setattr(credit_balance, "_session", db)

            return credit_balance

        except Exception as e:
            db.rollback()
            logger.error(f"Error adding credits to {address}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

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
        db = SessionLocal()

        try:
            # Get active transactions in order of expiration date (oldest first)
            transactions: list[CreditTransaction] = (
                db.query(CreditTransaction)
                .filter(CreditTransaction.address == address, CreditTransaction.is_active == True)  # noqa: E712
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
                logger.warning(f"Insufficient credits for {address}: requested {amount}, missing {remaining_amount}")

            db.commit()
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Error using credits from {address}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def get_balance(address: str) -> float:
        """
        Get the current balance for a user.

        Args:
            address: User's blockchain address

        Returns:
            Current balance in USD
        """
        db = SessionLocal()

        try:
            # Get credit balance
            credit_balance = db.query(CreditBalance).filter(CreditBalance.address == address).first()
            if not credit_balance:
                return 0

            # Make db session available to calculate balance
            setattr(credit_balance, "_session", db)

            return credit_balance.balance

        except Exception as e:
            logger.error(f"Error getting balance for {address}: {str(e)}", exc_info=True)
            return 0
        finally:
            db.close()
