import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, TIMESTAMP, UUID, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.api_key import ApiKey


class LiberclawUser(Base):
    __tablename__ = "liberclaw_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    user_type: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False, default="free")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    api_keys: Mapped[list["ApiKey"]] = relationship("ApiKey", back_populates="liberclaw_user")

    __table_args__ = (UniqueConstraint("user_id", "user_type", name="unique_liberclaw_user"),)

    def __init__(self, user_id: str, user_type: str, tier: str = "free"):
        self.user_id = user_id
        self.user_type = user_type
        self.tier = tier
