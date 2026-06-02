import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, UUID, Boolean, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.plan_subscription_event import PlanSubscriptionEvent
    from src.models.user import User

# Statuses a subscription can be in. ``upgrading`` parks an old sub while a higher
# tier's checkout is pending (excluded from the one-active-sub index).
ACTIVE_STATUSES = ("pending", "active", "overdue")


class PlanSubscription(Base):
    """A recurring subscription to a paid tier, sold through a payment provider.

    Named ``plan_subscriptions`` (not ``subscriptions``) to avoid colliding with the
    legacy agent-charge subscriptions that were removed in Phase 0.
    """

    __tablename__ = "plan_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pending_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    is_trial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp()
    )

    user: Mapped["User"] = relationship("User", back_populates="plan_subscriptions")
    events: Mapped[list["PlanSubscriptionEvent"]] = relationship(
        "PlanSubscriptionEvent", back_populates="subscription", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # At most one live subscription per user (parked "upgrading" rows are excluded).
        Index(
            "uq_one_active_plan_subscription",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'active', 'overdue')"),
        ),
        Index("ix_plan_subscriptions_provider_subscription_id", "provider_subscription_id"),
    )

    def __init__(
        self,
        user_id: uuid.UUID,
        tier: str,
        provider: str,
        status: str = "pending",
        provider_subscription_id: str | None = None,
        provider_customer_id: str | None = None,
        currency: str | None = None,
        current_period_start: datetime | None = None,
        current_period_end: datetime | None = None,
        cancel_at_period_end: bool = False,
        pending_tier: str | None = None,
        is_trial: bool = False,
    ):
        self.user_id = user_id
        self.tier = tier
        self.provider = provider
        self.status = status
        self.provider_subscription_id = provider_subscription_id
        self.provider_customer_id = provider_customer_id
        self.currency = currency
        self.current_period_start = current_period_start
        self.current_period_end = current_period_end
        self.cancel_at_period_end = cancel_at_period_end
        self.pending_tier = pending_tier
        self.is_trial = is_trial
