import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, CheckConstraint, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

# entry_type values (plain strings, mirroring plan_subscription_event.event_type):
#   "seat_charge_prorated" | "monthly_renewal" | "extra_credits_usage" | "adjustment"


class TeamLedgerEntry(Base):
    """Debit side of the team statement (top-ups live in team_credit_transactions)."""

    __tablename__ = "team_ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    entry_type: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # positive USD debit
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    __table_args__ = (CheckConstraint("amount >= 0", name="check_ledger_amount_non_negative"),)

    def __init__(self, team_id: uuid.UUID, entry_type: str, amount: float, metadata_json: dict | None = None):
        self.team_id = team_id
        self.entry_type = entry_type
        self.amount = amount
        self.metadata_json = metadata_json
