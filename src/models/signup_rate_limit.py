import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class SignupRateLimit(Base):
    """Fixed-window counter for email sign-in/sign-up attempts, per IP and per network.

    Email-domain blocklists cannot catch attackers who rotate custom domains. This counts
    attempts by the source IP and its /24 (IPv4) or /64 (IPv6), so bulk account creation is
    throttled regardless of the address used. One row per ``(scope, value)``; the window
    opens on the first attempt and resets in place once it expires.
    """

    __tablename__ = "signup_rate_limits"
    __table_args__ = (UniqueConstraint("scope", "value", name="uq_signup_rate_limit_scope_value"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
