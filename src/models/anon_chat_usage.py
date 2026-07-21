import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, Integer, String, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class AnonChatUsage(Base):
    """Per-IP fixed-window message counter for anonymous (logged-out) chat.

    One row per client IP. The window opens on the first message, runs for a fixed
    duration, then resets in place once it expires. Keyed by IP (not a user) because
    anonymous users have no account/session — a soft nudge-to-login limit, not
    abuse-proof (shared/NAT IPs share a window; VPN/IP rotation bypasses it).
    """

    __tablename__ = "anon_chat_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    ip: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    window_started_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Second, longer fixed window (weekly cap) running independently of the daily one.
    week_started_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    week_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
