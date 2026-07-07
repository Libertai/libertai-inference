import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class TeamInvite(Base):
    """Email invite to a team. Token stored hashed (sha256), single-use via status."""

    __tablename__ = "team_invites"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)  # stored lowercased
    role: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    def __init__(self, team_id: uuid.UUID, email: str, role: str, token_hash: str, expires_at: datetime):
        self.team_id = team_id
        self.email = email.strip().lower()
        self.role = role
        self.token_hash = token_hash
        self.expires_at = expires_at
