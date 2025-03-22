from typing import TYPE_CHECKING

from sqlalchemy import Column, String, TIMESTAMP
from sqlalchemy.orm import relationship, Mapped
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.credit_transaction import CreditTransaction
    from src.models.credit_balance import CreditBalance


class User(Base):
    __tablename__ = "users"

    address = Column(String, primary_key=True)  # Unique address for Ethereum or Solana address
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

    credit_transactions: Mapped[list["CreditTransaction"]] = relationship(
        "CreditTransaction", back_populates="user", cascade="all, delete-orphan"
    )
    credit_balance: Mapped["CreditBalance"] = relationship(
        "CreditBalance", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
