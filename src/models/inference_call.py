import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    TIMESTAMP,
    UUID,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from src.models.base import Base

if TYPE_CHECKING:
    from src.models.api_key import ApiKey


class InferenceCall(Base):
    __tablename__ = "inference_calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_key_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    credits_used: Mapped[float] = mapped_column(Float, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    used_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    model_name: Mapped[str] = mapped_column(String, nullable=False)

    api_key: Mapped["ApiKey"] = relationship("ApiKey", back_populates="usages")

    # Enforce non-negative credits usage
    __table_args__ = (CheckConstraint("credits_used >= 0", name="check_credits_used_non_negative"),)

    def __init__(
            self, api_key_id: uuid.UUID, credits_used: float,
            input_tokens: int, output_tokens: int, cached_tokens: int,
            model_name: str
    ):
        self.api_key_id = api_key_id
        self.credits_used = credits_used
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.model_name = model_name
