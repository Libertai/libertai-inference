from typing import TYPE_CHECKING

from sqlalchemy import Column, String, Float, TIMESTAMP, ForeignKey, CheckConstraint, Integer
from sqlalchemy.orm import relationship, Mapped
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    transaction_hash = Column(String, primary_key=True)  # Unique transaction hash
    address = Column(String, ForeignKey("users.address", ondelete="CASCADE"), nullable=False)
    usd_value = Column(Float, nullable=False)
    block_number = Column(Integer, nullable=False)  # The block number this transaction was processed in
    created_at = Column(TIMESTAMP, default=func.current_timestamp())

    # Enforcing the non-negative amount constraint at the database level
    __table_args__ = (CheckConstraint("usd_value >= 0", name="check_usd_value_non_negative"),)

    user: Mapped["User"] = relationship("User", back_populates="credit_transactions")
