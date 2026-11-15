import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class SignupRateLimit(Base):
    """Fixed-window counter shared by signup and magic-link limits.

    Signup scopes count new-account attempts by source IP/network across email, wallet and
    OAuth. Magic-link scopes count sends by global day, source IP, canonical recipient and a
    short cooldown, so one mailbox cannot generate unlimited emails through aliases. One row
    per ``(scope, value)``; the window opens on the first attempt and resets in place once it
    expires.
    """

    __tablename__ = "signup_rate_limits"
    __table_args__ = (UniqueConstraint("scope", "value", name="uq_signup_rate_limit_scope_value"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
