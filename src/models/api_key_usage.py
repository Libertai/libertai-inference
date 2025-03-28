from datetime import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import String, Float, TIMESTAMP, ForeignKey, CheckConstraint, UUID
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.api_key import ApiKey


class ApiKeyUsage(Base):
    __tablename__ = "api_key_usages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_key_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    credits_used: Mapped[float] = mapped_column(Float, nullable=False)
    used_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    api_key: Mapped["ApiKey"] = relationship("ApiKey", back_populates="usages")

    # Enforce non-negative credits usage
    __table_args__ = (CheckConstraint("credits_used >= 0", name="check_credits_used_non_negative"),)

    def __init__(self, api_key_id: uuid.UUID, credits_used: float):
        self.api_key_id = api_key_id
        self.credits_used = credits_used
