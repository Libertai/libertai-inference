from src.models.base import SessionLocal
from src.models.credit_balance import CreditBalance
from src.models.credit_transaction import CreditTransaction
from src.models.user import User
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class CreditService:
    @staticmethod
    def add_credits(address: str, amount: float, transaction_hash: str, block_number: int):
        """
        Add credits to a user, creating the user if they don't exist.

        Args:
            address: User's blockchain address
            amount: USD amount to add
            transaction_hash: Transaction hash for recording the transaction
            block_number: The block number this transaction was processed in

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

            # Add credits
            credit_balance.balance += amount  # type: ignore

            # Record transaction with block number
            transaction = CreditTransaction(
                transaction_hash=transaction_hash,
                address=address,
                usd_value=amount,  # type: ignore
                block_number=block_number,
            )
            db.add(transaction)

            db.commit()
            return credit_balance

        except Exception as e:
            db.rollback()
            logger.error(f"Error adding credits to {address}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()
