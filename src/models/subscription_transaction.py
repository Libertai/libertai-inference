import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import String, TIMESTAMP, ForeignKey, Float, UUID, Enum
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.subscription import Subscription


class SubscriptionTransactionStatus(str, enum.Enum):
    success = "success"
    failed = "failed"


class SubscriptionTransaction(Base):
    __tablename__ = "subscription_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[SubscriptionTransactionStatus] = mapped_column(
        Enum(SubscriptionTransactionStatus), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    
    # Optional notes about the transaction
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    subscription: Mapped["Subscription"] = relationship("Subscription", back_populates="transactions")

    def __init__(
        self,
        subscription_id: uuid.UUID,
        amount: float,
        status: SubscriptionTransactionStatus,
        notes: Optional[str] = None,
    ):
        self.subscription_id = subscription_id
        self.amount = amount
        self.status = status
        self.notes = notes