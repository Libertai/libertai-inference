import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class WalletChallenge(Base):
    """A short-lived nonce a wallet must sign to prove address ownership at login."""

    __tablename__ = "wallet_challenges"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    address: Mapped[str] = mapped_column(String, nullable=False, index=True)
    nonce: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    def __init__(self, address: str, nonce: str, expires_at: datetime):
        self.address = address
        self.nonce = nonce
        self.expires_at = expires_at
