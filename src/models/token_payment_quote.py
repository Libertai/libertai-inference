import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, UUID, CheckConstraint, Float, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class TokenPaymentQuote(Base):
    """A server-issued price lock for an on-chain top-up.

    A payment from ``payer_address`` of exactly ``token_amount_raw`` mined before ``expires_at``
    is credited ``credit_amount`` instead of being valued at the price when the watcher sees it.
    Single use: ``external_reference`` is the payment that consumed it.
    """

    __tablename__ = "token_payment_quotes"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chain: Mapped[str] = mapped_column(String, nullable=False)  # LibertaiChain value
    token: Mapped[str] = mapped_column(String, nullable=False)  # QuoteToken value
    # Same format as the watchers' format_address output, so lookups are exact matches.
    payer_address: Mapped[str] = mapped_column(String, nullable=False)
    # Credits granted on match, LTAI boost included.
    credit_amount: Mapped[float] = mapped_column(Float, nullable=False)
    token_price: Mapped[float] = mapped_column(Float, nullable=False)
    # Smallest token unit, as emitted by the payment event.
    token_amount_raw: Mapped[Decimal] = mapped_column(Numeric(78, 0), nullable=False)
    # Naive UTC, like every other timestamp in the schema.
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)

    __table_args__ = (
        CheckConstraint("credit_amount > 0", name="check_token_payment_quote_credit_positive"),
        CheckConstraint("token_amount_raw > 0", name="check_token_payment_quote_amount_positive"),
        CheckConstraint("expires_at > created_at", name="check_token_payment_quote_ordered"),
        Index("ix_token_payment_quotes_match", "payer_address", "token_amount_raw"),
    )

    def __init__(
        self,
        user_id: uuid.UUID,
        chain: str,
        token: str,
        payer_address: str,
        credit_amount: float,
        token_price: float,
        token_amount_raw: Decimal,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        self.user_id = user_id
        self.chain = chain
        self.token = token
        self.payer_address = payer_address
        self.credit_amount = credit_amount
        self.token_price = token_price
        self.token_amount_raw = token_amount_raw
        self.created_at = created_at
        self.expires_at = expires_at
