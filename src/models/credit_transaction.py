from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, Float, TIMESTAMP, ForeignKey, CheckConstraint, Integer, Boolean
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    transaction_hash: Mapped[str] = mapped_column(String, primary_key=True)  # Unique transaction hash
    address: Mapped[str] = mapped_column(String, ForeignKey("users.address", ondelete="CASCADE"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    amount_left: Mapped[float] = mapped_column(
        Float, nullable=False
    )  # Remaining amount available from this transaction
    provider: Mapped[str] = mapped_column(String, nullable=False)
    block_number: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # The block number this transaction was processed in
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    expired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)  # Optional expiration date
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )  # Whether the credits of this transaction are still active

    def __init__(
        self,
        transaction_hash: str,
        address: str,
        amount: float,
        amount_left: float,
        provider: str,
        block_number: int | None = None,
        expired_at: datetime | None = None,
        is_active: bool = True,
    ):
        self.transaction_hash = transaction_hash
        self.address = address
        self.amount = amount
        self.amount_left = amount_left
        self.provider = provider
        self.block_number = block_number
        self.expired_at = expired_at
        self.is_active = is_active

    # Enforcing constraints at the database level
    __table_args__ = (
        CheckConstraint("amount >= 0", name="check_amount_non_negative"),
        CheckConstraint("amount_left >= 0", name="check_amount_left_non_negative"),
        CheckConstraint("amount_left <= amount", name="check_amount_left_not_exceeding_value"),
        CheckConstraint("provider IN ('libertai', 'thirdweb')", name="check_provider_choices"),
        CheckConstraint(
            "(provider = 'thirdweb') OR (provider = 'libertai' AND block_number IS NOT NULL)",
            name="check_block_number_required_for_provider_libertai",
        ),
    )

    user: Mapped["User"] = relationship("User", back_populates="credit_transactions")

    @property
    def used_amount(self) -> float:
        """
        Calculate amount used from this transaction.
        """
        return max(0.0, self.amount - self.amount_left)
