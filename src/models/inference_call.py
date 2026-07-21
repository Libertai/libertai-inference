import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    TIMESTAMP,
    UUID,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
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
    # Portion of credits_used covered by the tier's entitlement windows; the rest was
    # paid from prepaid balance. Window usage sums this column, so prepaid-paid usage
    # never drains the allowance.
    tier_credits_used: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    # Liberclaw keys only: portion of credits_used paid from granted extra credits
    # (liberclaw_credit_grants) after the tier's rolling-window cap was exhausted.
    # NULL on every other key type and on within-cap liberclaw calls; liberclaw
    # window usage sums credits_used minus this, so grant-paid overflow never
    # drains the rolling allowance.
    liberclaw_extra_credits_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    model_name: Mapped[str] = mapped_column(String, nullable=False)

    api_key: Mapped["ApiKey"] = relationship("ApiKey", back_populates="usages")

    # Enforce non-negative credits usage; index the usage-rollup access path
    # (SUM(credits_used) WHERE api_key_id = ? AND used_at >= ?).
    __table_args__ = (
        CheckConstraint("credits_used >= 0", name="check_credits_used_non_negative"),
        CheckConstraint("tier_credits_used >= 0", name="check_tier_credits_used_non_negative"),
        CheckConstraint(
            "liberclaw_extra_credits_used IS NULL OR liberclaw_extra_credits_used >= 0",
            name="check_liberclaw_extra_credits_used_non_negative",
        ),
        Index("ix_inference_calls_api_key_id_used_at", "api_key_id", "used_at"),
    )

    def __init__(
        self,
        api_key_id: uuid.UUID,
        credits_used: float,
        model_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        image_count: int = 0,
        tier_credits_used: float = 0.0,
        liberclaw_extra_credits_used: float | None = None,
    ):
        self.api_key_id = api_key_id
        self.credits_used = credits_used
        self.model_name = model_name
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.image_count = image_count
        self.tier_credits_used = tier_credits_used
        self.liberclaw_extra_credits_used = liberclaw_extra_credits_used
