import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class MagicLink(Base):
    """A pending passwordless email login (signed token + 6-digit code, both hashed)."""

    __tablename__ = "magic_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    code_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    def __init__(self, email: str, token_hash: str, expires_at: datetime, code_hash: str | None = None):
        self.email = email
        self.token_hash = token_hash
        self.expires_at = expires_at
        self.code_hash = code_hash
