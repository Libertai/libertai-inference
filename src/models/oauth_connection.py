import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, UUID, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class OAuthConnection(Base):
    """A linked OAuth identity (e.g. Google, GitHub) pointing at a user."""

    __tablename__ = "oauth_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    provider_email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    user: Mapped["User"] = relationship("User", back_populates="oauth_connections")

    __table_args__ = (UniqueConstraint("provider", "provider_id", name="uq_oauth_connection_provider_id"),)

    def __init__(self, user_id: uuid.UUID, provider: str, provider_id: str, provider_email: str | None = None):
        self.user_id = user_id
        self.provider = provider
        self.provider_id = provider_id
        self.provider_email = provider_email
