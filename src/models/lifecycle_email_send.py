import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, Boolean, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LifecycleEmailSend(Base):
    """Log of lifecycle emails sent to a user; drives dedup, frequency caps and reporting."""

    __tablename__ = "lifecycle_email_sends"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    email_type: Mapped[str] = mapped_column(String, nullable=False)
    # Transactional sends bypass opt-out and the frequency cap, so they must not count toward it.
    transactional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, default=func.current_timestamp())

    __table_args__ = (
        Index("ix_lifecycle_email_sends_user_type", "user_id", "email_type"),
        Index("ix_lifecycle_email_sends_user_sent_at", "user_id", "sent_at"),
    )

    def __init__(self, user_id: uuid.UUID, email_type: str, transactional: bool = False):
        self.user_id = user_id
        self.email_type = email_type
        self.transactional = transactional
