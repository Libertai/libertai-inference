import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, TIMESTAMP, ForeignKey, func, UUID
from sqlalchemy.orm import relationship, Mapped, mapped_column

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User
    from src.models.subscription import Subscription


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    instance_hash: Mapped[str | None] = mapped_column(
        String, unique=True, nullable=True, index=True
    )  # None when not running (subscription inactive)
    name: Mapped[str] = mapped_column(String, nullable=False)
    user_address: Mapped[str] = mapped_column(String, ForeignKey("users.address", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    ssh_public_key: Mapped[str] = mapped_column(String)

    user: Mapped["User"] = relationship("User", back_populates="agents")
    subscription_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("subscriptions.id"), nullable=False)
    subscription: Mapped["Subscription"] = relationship("Subscription", foreign_keys=[subscription_id])

    def __init__(
        self,
        instance_hash: str,
        name: str,
        user_address: str,
        ssh_public_key: str,
        agent_id: uuid.UUID = uuid.uuid4(),
    ):
        self.id = agent_id
        self.instance_hash = instance_hash
        self.name = name
        self.user_address = user_address
        self.ssh_public_key = ssh_public_key
