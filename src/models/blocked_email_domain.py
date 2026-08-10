from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class BlockedEmailDomain(Base):
    """A domain refused at account creation. Rows are inserted operationally, never seeded from code."""

    __tablename__ = "blocked_email_domains"
    # Matching is exact, so a row that is not already normalised would never match anything.
    __table_args__ = (
        CheckConstraint("domain = lower(btrim(domain))", name="blocked_email_domains_domain_normalized"),
    )

    # Lowercase apex, matched exactly: no subdomain descent and no wildcards.
    domain: Mapped[str] = mapped_column(String, primary_key=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # server_default, not default: rows arrive by hand-written INSERT that omits this column.
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())

    def __init__(self, domain: str, reason: str | None = None):
        self.domain = domain
        self.reason = reason
