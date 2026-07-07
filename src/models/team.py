import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, Float, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class Team(Base):
    """A sales-provisioned organization whose credit balance funds member seats.

    ``seat_prices`` maps tier name -> negotiated monthly USD price. A tier absent
    from a NON-empty map is not sellable to this team; an empty map means list
    prices. Caps of ``None`` are treated as 0 (extra credits disabled).
    """

    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")  # "active" | "suspended"
    seat_prices: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    extra_credits_monthly_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    extra_credits_member_default_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp()
    )

    def __init__(self, name: str):
        self.name = name
        self.status = "active"
        self.seat_prices = {}
