import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, UUID, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.api_key import ApiKey
    from src.models.liberclaw_credit_grant import LiberclawCreditGrant


class LiberclawUser(Base):
    __tablename__ = "liberclaw_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    user_type: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False, default="free")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    # LiberClaw's own users.id, set by the api-key call. Identity bridge to Invoice.liberclaw_account_id
    # (never key ownership on this row's user_id/user_type — that's the email-based identity, and emails recycle).
    liberclaw_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True, index=True)

    api_keys: Mapped[list["ApiKey"]] = relationship("ApiKey", back_populates="liberclaw_user")
    credit_grants: Mapped[list["LiberclawCreditGrant"]] = relationship(
        "LiberclawCreditGrant", back_populates="liberclaw_user"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "user_type", name="unique_liberclaw_user"),
        # Partial: many rows have no liberclaw_account_id yet (legacy/backfill-pending).
        Index(
            "uq_liberclaw_users_account_id",
            "liberclaw_account_id",
            unique=True,
            postgresql_where=text("liberclaw_account_id IS NOT NULL"),
        ),
    )

    def __init__(
        self, user_id: str, user_type: str, tier: str = "free", liberclaw_account_id: uuid.UUID | None = None
    ):
        self.user_id = user_id
        self.user_type = user_type
        self.tier = tier
        self.liberclaw_account_id = liberclaw_account_id
