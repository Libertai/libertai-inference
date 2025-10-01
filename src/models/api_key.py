import secrets
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, TIMESTAMP, ForeignKey, Float, Boolean, func, UniqueConstraint, UUID, Enum
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql.expression import func as sql_func

from src.interfaces.api_keys import ApiKeyType
from src.models.base import Base, SessionLocal

if TYPE_CHECKING:
    from src.models.user import User
    from src.models.inference_call import InferenceCall
    from src.models.chat_request import ChatRequest


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    user_address: Mapped[str] = mapped_column(String, ForeignKey("users.address", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monthly_limit: Mapped[float | None] = mapped_column(Float, nullable=True)  # Credits limit per month
    type: Mapped[ApiKeyType] = mapped_column(Enum(ApiKeyType), nullable=False, default=ApiKeyType.api)

    user: Mapped["User"] = relationship("User", back_populates="api_keys")
    usages: Mapped[list["InferenceCall"]] = relationship(
        "InferenceCall", back_populates="api_key", cascade="all, delete-orphan"
    )
    chat_requests: Mapped[list["ChatRequest"]] = relationship(
        "ChatRequest", back_populates="api_key", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("user_address", "name", name="unique_api_key_name_per_user"),)

    def __init__(
        self,
        key: str,
        name: str,
        user_address: str,
        monthly_limit: float | None = None,
        type: ApiKeyType = ApiKeyType.api,
    ):
        self.key = key
        self.name = name
        self.user_address = user_address
        self.monthly_limit = monthly_limit
        self.type = type

    @property
    def masked_key(self) -> str:
        """Return a masked version of the API key, showing only the first 4 and last 4 characters."""
        if len(self.key) <= 8:
            return "****"
        return f"{self.key[:4]}...{self.key[-4:]}"

    @staticmethod
    def generate_key() -> str:
        """Generate a random API key ID."""
        return secrets.token_hex(16)

    @property
    def current_month_usage(self) -> float:
        """
        Calculate the total usage for the current month directly from the database.
        """
        from src.models.inference_call import InferenceCall

        with SessionLocal() as db:
            # Get current month's usage using SQL aggregation
            now = datetime.now()
            first_day = datetime(now.year, now.month, 1)
            next_month = datetime(now.year + (now.month // 12), ((now.month % 12) + 1), 1)

            result = (
                db.query(sql_func.sum(InferenceCall.credits_used))
                .filter(
                    InferenceCall.api_key_id == self.id,
                    InferenceCall.used_at >= first_day,
                    InferenceCall.used_at < next_month,
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
        from src.services.credit import CreditService

        # Get user's current balance
        user_balance = CreditService.get_balance(self.user_address)

        # If there's a monthly limit, calculate remaining credits within that limit
        if self.monthly_limit is not None:
            limit_remaining = max(0.0, self.monthly_limit - self.current_month_usage)
            # Use the minimum of remaining limit and available balance
            return min(limit_remaining, user_balance)

        # If no monthly limit is set, the effective limit is just the user's available balance
        return user_balance
