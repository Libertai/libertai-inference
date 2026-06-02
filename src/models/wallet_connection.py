import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, UUID, Boolean, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class WalletConnection(Base):
    """A blockchain wallet linked to a user. Email/OAuth-only users have none."""

    __tablename__ = "wallet_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chain: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(String, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    user: Mapped["User"] = relationship("User", back_populates="wallet_connections")

    __table_args__ = (UniqueConstraint("chain", "address", name="uq_wallet_connection_chain_address"),)

    def __init__(self, user_id: uuid.UUID, chain: str, address: str, is_primary: bool = False):
        self.user_id = user_id
        self.chain = chain
        self.address = address
        self.is_primary = is_primary
