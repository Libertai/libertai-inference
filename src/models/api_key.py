import secrets
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, TIMESTAMP, ForeignKey, Float, Boolean, func, UniqueConstraint
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql.expression import func as sql_func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User
    from src.models.api_key_usage import ApiKeyUsage


class ApiKey(Base):
    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(String, ForeignKey("users.address", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monthly_limit: Mapped[float | None] = mapped_column(Float, nullable=True)  # Credits limit per month

    user: Mapped["User"] = relationship("User", back_populates="api_keys")
    usages: Mapped[list["ApiKeyUsage"]] = relationship(
        "ApiKeyUsage", back_populates="api_key", cascade="all, delete-orphan"
    )

    __table_args__ = UniqueConstraint("address", "name", name="unique_api_key_name_per_user")

    def __init__(self, key_id: str, name: str, address: str, monthly_limit: float | None = None):
        self.key_id = key_id
        self.name = name
        self.address = address
        self.monthly_limit = monthly_limit

    @staticmethod
    def generate_key_id() -> str:
        """Generate a random API key ID."""
        return secrets.token_hex(16)

    @property
    def current_month_usage(self) -> float:
        """
        Calculate the total usage for the current month directly from the database.
        """
        if not hasattr(self, "_session"):
            # When not in a session context, we can't calculate
            return 0.0

        from src.models.api_key_usage import ApiKeyUsage

        # Get current month's usage using SQL aggregation
        now = datetime.now()
        first_day = datetime(now.year, now.month, 1)
        next_month = datetime(now.year + (now.month // 12), ((now.month % 12) + 1), 1)

        result = (
            self._session.query(sql_func.sum(ApiKeyUsage.credits_used))
            .filter(
                ApiKeyUsage.key_id == self.key_id, ApiKeyUsage.used_at >= first_day, ApiKeyUsage.used_at < next_month
            )
            .scalar()
        )

        return float(result or 0.0)

    @property
    def effective_limit_remaining(self) -> float:
        """
        Calculate effective remaining usage based on both the API key's monthly limit
        and the user's available credit balance.

        This combines the monthly limit of the API key with the user's current balance
        to determine how many credits can actually be used.
        """
        if not hasattr(self, "_session"):
            return 0.0

        # Import here to avoid circular imports
        from src.services.credit_service import CreditService

        # Get user's current balance
        user_balance = CreditService.get_balance(self.address)

        # If there's a monthly limit, calculate remaining credits within that limit
        if self.monthly_limit is not None:
            limit_remaining = max(0.0, self.monthly_limit - self.current_month_usage)
            # Use the minimum of remaining limit and available balance
            return min(limit_remaining, user_balance)

        # If no monthly limit is set, the effective limit is just the user's available balance
        return user_balance
