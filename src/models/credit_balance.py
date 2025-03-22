from typing import TYPE_CHECKING

from sqlalchemy import Column, String, Float, ForeignKey, CheckConstraint
from sqlalchemy.orm import relationship, Mapped

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class CreditBalance(Base):
    __tablename__ = "credit_balances"

    address = Column(String, ForeignKey("users.address", ondelete="CASCADE"), primary_key=True)
    balance = Column(Float, nullable=False, default=0)

    # Enforcing the non-negative balance constraint at the database level
    __table_args__ = (CheckConstraint("balance >= 0", name="check_balance_non_negative"),)

    user: Mapped["User"] = relationship("User", back_populates="credit_balance")
