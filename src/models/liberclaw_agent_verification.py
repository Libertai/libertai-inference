"""One-off $1 Revolut orders proving a free LiberClaw account is not a bot.

The order is created through the same Merchant API account and settled by the
same webhook as top-ups. On completion the dollar is granted as a LiberClaw
extra-credit grant (USD-denominated), so no cross-service credit endpoint is
needed.
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models.base import Base


class LiberclawAgentVerification(Base):
    __tablename__ = "liberclaw_agent_verifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    # LiberClaw's immutable users.id. No FK: it lives in another database.
    liberclaw_account_id: Mapped[uuid.UUID] = mapped_column(UUID, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False, default="revolut")
    provider_order_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    checkout_url: Mapped[str | None] = mapped_column(String, nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    # pending | paid | failed | conflict
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # A card may verify only one account. NULL (Revolut Pay, bank) is allowed.
    payment_method_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    credited_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, default=func.current_timestamp(), server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        nullable=False,
        default=func.current_timestamp(),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (CheckConstraint("amount_cents > 0", name="check_liberclaw_verification_amount_positive"),)

    def __init__(
        self,
        liberclaw_account_id: uuid.UUID,
        provider: str,
        provider_order_id: str,
        amount_cents: int,
        currency: str,
        status: str = "pending",
        checkout_url: str | None = None,
        payment_method_fingerprint: str | None = None,
    ):
        self.liberclaw_account_id = liberclaw_account_id
        self.provider = provider
        self.provider_order_id = provider_order_id
        self.amount_cents = amount_cents
        self.currency = currency
        self.status = status
        self.checkout_url = checkout_url
        self.payment_method_fingerprint = payment_method_fingerprint
