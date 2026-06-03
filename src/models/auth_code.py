import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, UUID, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class AuthCode(Base):
    """One-time code exchanged for a token pair (OAuth callback -> frontend/CLI SSO).

    Holds the Fernet-encrypted access/refresh tokens; short-lived and single-use.
    """

    __tablename__ = "auth_codes"

    code_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    access_token: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    # PKCE: SHA256(code_verifier) supplied when the code was minted (CLI loopback flow).
    # Null for OAuth-callback codes, which are not PKCE-bound.
    challenge: Mapped[str | None] = mapped_column(String, nullable=True)

    user: Mapped["User"] = relationship("User")

    def __init__(
        self,
        code_hash: str,
        user_id: uuid.UUID,
        access_token: str,
        refresh_token: str,
        expires_at: datetime,
        challenge: str | None = None,
    ):
        self.code_hash = code_hash
        self.user_id = user_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.challenge = challenge
