import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from dateutil.relativedelta import relativedelta
from sqlalchemy import String, TIMESTAMP, ForeignKey, Float, func, UUID, CheckConstraint, Enum
from sqlalchemy.orm import relationship, Mapped, mapped_column

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User
    from src.models.subscription_transaction import SubscriptionTransaction


class SubscriptionType(str, enum.Enum):
    agent = "agent"


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    cancelled = "cancelled"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_address: Mapped[str] = mapped_column(String, ForeignKey("users.address", ondelete="CASCADE"), nullable=False)
    subscription_type: Mapped[SubscriptionType] = mapped_column(Enum(SubscriptionType), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # Amount to charge per period
    last_charged_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    next_charge_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), nullable=False, default=SubscriptionStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    # Related entity ID (could be agent_id, feature_id, etc.)
    related_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="subscriptions")
    transactions: Mapped[list["SubscriptionTransaction"]] = relationship(
        "SubscriptionTransaction", back_populates="subscription", cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint("amount >= 0", name="check_subscription_amount_non_negative"),)

    def __init__(
        self,
        user_address: str,
        subscription_type: SubscriptionType,
        amount: float,
        related_id: uuid.UUID,
        next_charge_at: datetime | None = None,
        status: SubscriptionStatus = SubscriptionStatus.active,
    ):
        self.user_address = user_address
        self.subscription_type = subscription_type
        self.amount = amount
        self.related_id = related_id

        # If next_charge_at is not provided, calculate it based on period
        current_time = datetime.now()
        self.last_charged_at = current_time

        if next_charge_at:
            self.next_charge_at = next_charge_at
        else:
            self.next_charge_at = current_time + relativedelta(months=1)

        self.status = status

    def pause(self) -> None:
        """Pause this subscription"""
        self.status = SubscriptionStatus.paused

    def resume(self) -> None:
        """Resume this subscription"""
        self.status = SubscriptionStatus.active

    def cancel(self) -> None:
        """Cancel this subscription"""
        self.status = SubscriptionStatus.cancelled

    def update_charge_dates(self, timestamp: datetime | None = None, months: int = 1) -> None:
        """
        Update the last_charged_at and next_charge_at dates after a successful charge

        Args:
            timestamp: When the charge happened, defaults to current time
            months: Number of months to add for the next charge date
        """
        if timestamp is None:
            timestamp = datetime.now()

        self.last_charged_at = timestamp

        # Calculate next charge date based on the period
        self.next_charge_at = timestamp + relativedelta(months=months)
