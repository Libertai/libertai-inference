import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, UUID, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class EntitlementWindow(Base):
    """A fixed (non-rolling) usage window for a user.

    A window opens when the user sends a message (no active window of that kind),
    runs for a fixed duration (5h or weekly), accumulates usage against the tier
    limit, then resets: once ``expires_at`` passes the window is treated as empty
    and the next message opens a fresh one starting at that moment. One row per
    (user, kind), reset in place.
    """

    __tablename__ = "entitlement_windows"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "5h" | "weekly"
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "kind", name="uq_entitlement_window_user_kind"),)

    def __init__(self, user_id: uuid.UUID, kind: str, started_at: datetime, expires_at: datetime):
        self.user_id = user_id
        self.kind = kind
        self.started_at = started_at
        self.expires_at = expires_at
