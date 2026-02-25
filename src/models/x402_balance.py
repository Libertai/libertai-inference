from datetime import datetime

from sqlalchemy import TIMESTAMP, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models.base import Base


class X402Balance(Base):
    __tablename__ = "x402_balances"

    user_address: Mapped[str] = mapped_column(
        String, ForeignKey("users.address", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp()
    )
