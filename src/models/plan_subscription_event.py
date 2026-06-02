import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, TIMESTAMP, UUID, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.plan_subscription import PlanSubscription


class PlanSubscriptionEvent(Base):
    """Audit log of subscription lifecycle events; ``provider_event_id`` dedups webhooks."""

    __tablename__ = "plan_subscription_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("plan_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    subscription: Mapped["PlanSubscription"] = relationship("PlanSubscription", back_populates="events")

    __table_args__ = (
        UniqueConstraint("provider_event_id", name="uq_plan_subscription_event_provider_event_id"),
    )

    def __init__(
        self,
        subscription_id: uuid.UUID,
        event_type: str,
        provider_event_id: str | None = None,
        metadata_json: dict | None = None,
    ):
        self.subscription_id = subscription_id
        self.event_type = event_type
        self.provider_event_id = provider_event_id
        self.metadata_json = metadata_json
