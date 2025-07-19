import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, Float, TIMESTAMP, ForeignKey, CheckConstraint, Integer, Boolean, UUID, Enum
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql import func

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    def __repr__(self):
        # Get all mapped columns and their values
        attrs = []
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, str):
                attrs.append(f"{column.name}='{value}'")
            else:
                attrs.append(f"{column.name}={value}")

        return f"{self.__class__.__name__}({', '.join(attrs)})"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)  # Primary key UUID
    transaction_hash: Mapped[str | None] = mapped_column(
        String, nullable=True, unique=True
    )  # Optional transaction hash
    address: Mapped[str] = mapped_column(String, ForeignKey("users.address", ondelete="CASCADE"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    amount_left: Mapped[float] = mapped_column(
        Float, nullable=False
    )  # Remaining amount available from this transaction
    provider: Mapped[CreditTransactionProvider] = mapped_column(Enum(CreditTransactionProvider), nullable=False)
    block_number: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # The block number this transaction was processed in
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    expired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)  # Optional expiration date
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )  # Whether the credits of this transaction are still active
    status: Mapped[CreditTransactionStatus] = mapped_column(
        Enum(CreditTransactionStatus), nullable=False, default=CreditTransactionStatus.completed
    )  # Status of the transaction

    def __init__(
        self,
        address: str,
        amount: float,
        amount_left: float,
        provider: CreditTransactionProvider,
        transaction_hash: str | None = None,
        block_number: int | None = None,
        expired_at: datetime | None = None,
        is_active: bool = True,
        status: CreditTransactionStatus = CreditTransactionStatus.completed,
    ):
        self.transaction_hash = transaction_hash
        self.address = address
        self.amount = amount
        self.amount_left = amount_left
        self.provider = provider
        self.block_number = block_number
        self.expired_at = expired_at
        self.is_active = is_active
        self.status = status

    # Enforcing constraints at the database level
    __table_args__ = (
        CheckConstraint("amount >= 0", name="check_amount_non_negative"),
        CheckConstraint("amount_left >= 0", name="check_amount_left_non_negative"),
        CheckConstraint("amount_left <= amount", name="check_amount_left_not_exceeding_value"),
        CheckConstraint(
            "(provider::text = 'thirdweb' OR provider::text = 'voucher') OR (provider::text = 'base' AND block_number IS NOT NULL) OR (provider::text = 'solana' AND block_number IS NOT NULL)",
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
