from typing import TYPE_CHECKING

from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship, Mapped

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class CreditBalance(Base):
    __tablename__ = "credit_balances"

    address = Column(String, ForeignKey("users.address", ondelete="CASCADE"), primary_key=True)
    user: Mapped["User"] = relationship("User", back_populates="credit_balance")

    @property
    def balance(self) -> float:
        """
        Dynamically calculate the balance from active transactions.
        This will be calculated when accessing the property.
        """
        from src.models.credit_transaction import CreditTransaction

        # Get all active transactions for this user
        if not hasattr(self, "_session"):
            # When not in a session context, we can't calculate
            return 0.0

        active_transactions = (
            self._session.query(CreditTransaction)
            .filter(CreditTransaction.address == self.address, CreditTransaction.is_active == True)
            .all()
        )

        # Sum remaining amounts
        total_balance = sum(tx.amount_left for tx in active_transactions)
        return total_balance
