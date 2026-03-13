from typing import TYPE_CHECKING

from sqlalchemy import Column, String, TIMESTAMP, select, func
from sqlalchemy.orm import relationship, Mapped

from src.models.base import Base, AsyncSessionLocal

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

    async def get_credit_balance(self) -> float:
        from src.models.credit_transaction import CreditTransaction, CreditTransactionStatus

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                    CreditTransaction.address == self.address,
                    CreditTransaction.is_active == True,  # noqa: E712
                    CreditTransaction.status == CreditTransactionStatus.completed,
                )
            )
            return float(result.scalar() or 0.0)
