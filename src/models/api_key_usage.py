from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, Float, TIMESTAMP, ForeignKey, CheckConstraint
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.api_key import ApiKey


class ApiKeyUsage(Base):
    __tablename__ = "api_key_usages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, ForeignKey("api_keys.key", ondelete="CASCADE"), nullable=False)
    credits_used: Mapped[float] = mapped_column(Float, nullable=False)
    used_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    api_key: Mapped["ApiKey"] = relationship("ApiKey", back_populates="usages")

    # Enforce non-negative credits usage
    __table_args__ = (CheckConstraint("credits_used >= 0", name="check_credits_used_non_negative"),)

    def __init__(self, key: str, credits_used: float):
        self.key = key
        self.credits_used = credits_used
