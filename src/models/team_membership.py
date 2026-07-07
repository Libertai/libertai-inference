import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"


class TeamMembership(Base):
    """Pure membership (role + caps); seat state lives on plan_subscriptions.

    ``user_id`` is UNIQUE: one team per user. ``extra_credits_cap_override``
    of None falls back to the team's member default cap.
    """

    __tablename__ = "team_memberships"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    role: Mapped[str] = mapped_column(String, nullable=False, default=ROLE_MEMBER)
    extra_credits_cap_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())

    def __init__(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str = ROLE_MEMBER,
        invited_by: uuid.UUID | None = None,
    ):
        self.team_id = team_id
        self.user_id = user_id
        self.role = role
        self.invited_by = invited_by
