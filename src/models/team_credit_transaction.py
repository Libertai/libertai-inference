import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, Boolean, CheckConstraint, Enum, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import Base


class TeamCreditTransaction(Base):
    """A team-balance top-up, drained in place via ``amount_left`` (same algorithm
    as ``credit_transactions``; kept as a separate table so user-balance queries
    and constraints stay untouched)."""

    __tablename__ = "team_credit_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    amount_left: Mapped[float] = mapped_column(Float, nullable=False)
    provider: Mapped[CreditTransactionProvider] = mapped_column(Enum(CreditTransactionProvider), nullable=False)
    status: Mapped[CreditTransactionStatus] = mapped_column(
        Enum(CreditTransactionStatus), nullable=False, default=CreditTransactionStatus.completed
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    __table_args__ = (
        CheckConstraint("amount >= 0", name="check_team_tx_amount_non_negative"),
        CheckConstraint("amount_left >= 0", name="check_team_tx_amount_left_non_negative"),
        CheckConstraint("amount_left <= amount", name="check_team_tx_amount_left_not_exceeding"),
    )

    def __init__(
        self,
        team_id: uuid.UUID,
        amount: float,
        amount_left: float,
        provider: CreditTransactionProvider,
        external_reference: str | None = None,
        status: CreditTransactionStatus = CreditTransactionStatus.completed,
        is_active: bool = True,
    ):
        self.team_id = team_id
        self.amount = amount
        self.amount_left = amount_left
        self.provider = provider
        self.external_reference = external_reference
        self.status = status
        self.is_active = is_active
