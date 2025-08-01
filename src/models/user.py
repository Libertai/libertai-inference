from typing import TYPE_CHECKING

from sqlalchemy import Column, String, TIMESTAMP
from sqlalchemy.orm import relationship, Mapped
from sqlalchemy.sql import func

from src.models.base import Base, SessionLocal

if TYPE_CHECKING:
    from src.models.credit_transaction import CreditTransaction
    from src.models.api_key import ApiKey
    from src.models.agent import Agent
    from src.models.subscription import Subscription


class User(Base):
    __tablename__ = "users"

    address = Column(String, primary_key=True)  # Unique address for Ethereum or Solana address
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

    credit_transactions: Mapped[list["CreditTransaction"]] = relationship(
        "CreditTransaction", back_populates="user", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list["ApiKey"]] = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")
    agents: Mapped[list["Agent"]] = relationship("Agent", back_populates="user", cascade="all, delete-orphan")
    subscriptions: Mapped[list["Subscription"]] = relationship(
        "Subscription", back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def credit_balance(self) -> float:
        """
        Dynamically calculate the balance from active transactions.
        This will be calculated when accessing the property.
        """
        from src.models.credit_transaction import CreditTransaction, CreditTransactionStatus

        with SessionLocal() as db:
            # Get all active transactions for this user
            active_transactions = (
                db.query(CreditTransaction)
                .filter(
                    CreditTransaction.address == self.address,
                    CreditTransaction.is_active,
                    CreditTransaction.status == CreditTransactionStatus.completed,
                )
                .all()
            )

            # Sum remaining amounts
            total_balance = sum(tx.amount_left for tx in active_transactions)
            return total_balance
