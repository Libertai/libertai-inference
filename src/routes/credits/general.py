from datetime import datetime

from fastapi import HTTPException, status, Depends

from src.interfaces.credits import ExpiredCreditTransactionsResponse, ExpiredCreditTransaction, CreditBalanceResponse
from src.models.base import SessionLocal
from src.models.credit_transaction import CreditTransaction
from src.routes.credits import router
from src.services.auth import get_current_address
from src.services.credit import CreditService
from src.utils.cron import scheduler
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@scheduler.scheduled_job("interval", hours=1)
@router.post("/update-expired", description="Deactivate credits with a past expiration date.")  # type: ignore
async def update_expired_credit_transactions() -> ExpiredCreditTransactionsResponse:
    """
    Check for expired transactions and mark them as inactive.
    This can be called manually or via scheduled job.
    """

    db = SessionLocal()

    try:
        # Find transactions that have expired but still active
        expired_transactions: list[CreditTransaction] = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.is_active == True,  # noqa: E712
                CreditTransaction.expired_at.isnot(None),
                CreditTransaction.expired_at < datetime.now(),
            )
            .all()
        )

        if not expired_transactions:
            return ExpiredCreditTransactionsResponse(updated_count=0, transactions=[])

        # Update expired transactions
        transactions_response = []
        for tx in expired_transactions:
            tx.is_active = False
            transactions_response.append(
                ExpiredCreditTransaction(
                    transaction_hash=tx.transaction_hash, address=tx.address, expired_at=tx.expired_at
                )
            )
        db.commit()
        return ExpiredCreditTransactionsResponse(
            updated_count=len(expired_transactions), transactions=transactions_response
        )

    except Exception as e:
        db.rollback()
        logger.error(f"Error updating expired transactions: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error updating expired transactions: {str(e)}"
        )


@router.get("/balance", description="Get the current credit balance for authenticated user.")  # type: ignore
async def get_user_balance(user_address: str = Depends(get_current_address)) -> CreditBalanceResponse:
    """
    Get the current credit balance for the authenticated user.

    Returns:
        CreditBalanceResponse: Object containing the user's address and credit balance
    """
    try:
        balance = CreditService.get_balance(user_address)
        return CreditBalanceResponse(address=user_address, balance=balance)
    except Exception as e:
        logger.error(f"Error retrieving balance for {user_address}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error retrieving credit balance: {str(e)}"
        )
