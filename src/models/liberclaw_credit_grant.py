import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.liberclaw_user import LiberclawUser


class LiberclawCreditGrant(Base):
    """Extra usage credits granted to a Liberclaw user (e.g. the unused remainder
    of a plan cycle forfeited by a mid-cycle upgrade). Not purchasable; consumed
    by usage overflowing the tier's rolling-window cap. ``external_reference``
    makes grants idempotent across webhook retries."""

    __tablename__ = "liberclaw_credit_grants"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    liberclaw_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("liberclaw_users.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    amount_left: Mapped[float] = mapped_column(Float, nullable=False)
    external_reference: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # server_default so non-ORM inserts can never leave it NULL (consume order relies on it).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, default=func.current_timestamp(), server_default=func.current_timestamp()
    )

    liberclaw_user: Mapped["LiberclawUser"] = relationship("LiberclawUser", back_populates="credit_grants")

    __table_args__ = (
        CheckConstraint("amount > 0", name="check_liberclaw_grant_amount_positive"),
        CheckConstraint("amount_left >= 0", name="check_liberclaw_grant_amount_left_non_negative"),
        Index("ix_liberclaw_credit_grants_user_id", "liberclaw_user_id"),
    )

    def __init__(self, liberclaw_user_id: uuid.UUID, amount: float, external_reference: str):
        self.liberclaw_user_id = liberclaw_user_id
        self.amount = amount
        self.amount_left = amount
        self.external_reference = external_reference
