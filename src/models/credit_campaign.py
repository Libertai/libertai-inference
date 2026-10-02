import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    UUID,
    Boolean,
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.interfaces.campaigns import AlephGrantStatus
from src.models.base import Base


class CreditCampaign(Base):
    """A permanent claim code, typically printed as a QR code on a physical banner.

    The code itself never expires; claims are only accepted during its windows, so a
    reused banner is reactivated by adding a window rather than reprinting.
    """

    __tablename__ = "credit_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # Internal label (e.g. "Stand-up banner #1"), never shown to claimers.
    name: Mapped[str] = mapped_column(String, nullable=False)
    # USD credits per claim on each platform. aleph_amount 0 = LibertAI only.
    libertai_amount: Mapped[float] = mapped_column(Float, nullable=False)
    aleph_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    credit_validity_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # Kill switch: an inactive code accepts no claims whatever its windows say.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, default=func.current_timestamp(), server_default=func.current_timestamp()
    )

    windows: Mapped[list["CreditCampaignWindow"]] = relationship(
        "CreditCampaignWindow", back_populates="campaign", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("libertai_amount > 0", name="check_credit_campaign_libertai_amount_positive"),
        CheckConstraint("aleph_amount >= 0", name="check_credit_campaign_aleph_amount_non_negative"),
        CheckConstraint("credit_validity_days > 0", name="check_credit_campaign_validity_positive"),
    )

    def __init__(
        self, code: str, name: str, libertai_amount: float, aleph_amount: float, credit_validity_days: int
    ) -> None:
        self.code = code
        self.name = name
        self.libertai_amount = libertai_amount
        self.aleph_amount = aleph_amount
        self.credit_validity_days = credit_validity_days
        self.is_active = True


class CreditCampaignWindow(Base):
    """One event during which a campaign code accepts claims, with its own claim cap."""

    __tablename__ = "credit_campaign_windows"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("credit_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    event_name: Mapped[str] = mapped_column(String, nullable=False)
    # Naive UTC, like every other timestamp in the schema.
    starts_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    max_claims: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, default=func.current_timestamp(), server_default=func.current_timestamp()
    )

    campaign: Mapped["CreditCampaign"] = relationship("CreditCampaign", back_populates="windows")

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="check_credit_campaign_window_ordered"),
        CheckConstraint("max_claims > 0", name="check_credit_campaign_window_max_claims_positive"),
        Index("ix_credit_campaign_windows_campaign_id", "campaign_id"),
    )

    def __init__(
        self, campaign_id: uuid.UUID, event_name: str, starts_at: datetime, ends_at: datetime, max_claims: int
    ) -> None:
        self.campaign_id = campaign_id
        self.event_name = event_name
        self.starts_at = starts_at
        self.ends_at = ends_at
        self.max_claims = max_claims


class CreditCampaignClaim(Base):
    """One person's claim on a campaign code: the LibertAI voucher, plus the Aleph Cloud transfer
    once they give a wallet.

    One claim per account and per canonical email per code, across all its windows. The email is
    the anti-farming anchor: wallet-only accounts can't claim. Amounts and expiry are snapshotted
    so later campaign edits never rewrite history.
    """

    __tablename__ = "credit_campaign_claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("credit_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    window_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("credit_campaign_windows.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    email_canonical: Mapped[str] = mapped_column(String, nullable=False)
    credit_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("credit_transactions.id", ondelete="SET NULL"), nullable=True
    )
    libertai_amount: Mapped[float] = mapped_column(Float, nullable=False)
    # When both platforms' credits expire.
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)

    aleph_amount: Mapped[float] = mapped_column(Float, nullable=False)
    aleph_address: Mapped[str | None] = mapped_column(String, nullable=True)
    aleph_status: Mapped[AlephGrantStatus] = mapped_column(
        Enum(AlephGrantStatus, name="campaignalephstatus"), nullable=False, default=AlephGrantStatus.none
    )
    # The signed aleph_credit_transfer message, built once and resubmitted verbatim on retry:
    # same item_hash, so a retry after a lost response can never grant twice.
    aleph_message: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    aleph_item_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    aleph_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aleph_error: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, default=func.current_timestamp(), server_default=func.current_timestamp()
    )

    __table_args__ = (
        UniqueConstraint("campaign_id", "user_id", name="uq_credit_campaign_claims_user"),
        UniqueConstraint("campaign_id", "email_canonical", name="uq_credit_campaign_claims_email"),
        # NULLs are distinct: any number of claims may still be waiting for a wallet.
        UniqueConstraint("campaign_id", "aleph_address", name="uq_credit_campaign_claims_aleph_address"),
        Index("ix_credit_campaign_claims_window_id", "window_id"),
        Index("ix_credit_campaign_claims_aleph_status", "aleph_status"),
    )

    def __init__(
        self,
        campaign_id: uuid.UUID,
        window_id: uuid.UUID,
        user_id: uuid.UUID,
        email_canonical: str,
        libertai_amount: float,
        aleph_amount: float,
        expires_at: datetime,
    ) -> None:
        self.campaign_id = campaign_id
        self.window_id = window_id
        self.user_id = user_id
        self.email_canonical = email_canonical
        self.libertai_amount = libertai_amount
        self.aleph_amount = aleph_amount
        self.expires_at = expires_at
        self.aleph_status = AlephGrantStatus.none
        self.aleph_attempts = 0
