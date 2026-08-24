import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    UUID,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class Invoice(Base):
    """One issued invoice per paid Revolut order. Rows are immutable after insert
    (the one-time ``pdf`` fill excepted): corrections go through a credit note, never
    an edit, and rows must outlive the user (10-year retention) — hence RESTRICT."""

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    # LTAI-YYYY-NNNN; (year, seq) is the gap-free per-year sequence behind it.
    number: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, default=func.current_timestamp())
    payment_date: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    # Same convention as credit_transactions: "revolut:<order_id>". Idempotency key.
    external_reference: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    line_label: Mapped[str] = mapped_column(String, nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    seller: Mapped[dict] = mapped_column(JSON, nullable=False)
    buyer: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Rendered once, then served as stored bytes: the retained document must stay
    # byte-identical to what the customer downloaded, across template/lib upgrades.
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("year", "seq", name="uq_invoices_year_seq"),
        # Access path of the list endpoint (per-user, newest first); the table only ever grows.
        Index("ix_invoices_user_id_issued_at", "user_id", "issued_at"),
    )

    def __init__(
        self,
        number: str,
        year: int,
        seq: int,
        user_id: uuid.UUID,
        issued_at: datetime,
        payment_date: datetime,
        external_reference: str,
        currency: str,
        net_amount: Decimal,
        vat_amount: Decimal,
        gross_amount: Decimal,
        vat_rate: Decimal,
        line_label: str,
        seller: dict,
        buyer: dict,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ):
        self.number = number
        self.year = year
        self.seq = seq
        self.user_id = user_id
        self.issued_at = issued_at
        self.payment_date = payment_date
        self.external_reference = external_reference
        self.currency = currency
        self.net_amount = net_amount
        self.vat_amount = vat_amount
        self.gross_amount = gross_amount
        self.vat_rate = vat_rate
        self.line_label = line_label
        self.seller = seller
        self.buyer = buyer
        self.period_start = period_start
        self.period_end = period_end

    user: Mapped["User"] = relationship("User", back_populates="invoices")
