import secrets
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, TIMESTAMP, ForeignKey, Float, Boolean, func, UniqueConstraint, UUID, Enum, select
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql.expression import func as sql_func

from src.interfaces.api_keys import ApiKeyType
from src.models.base import Base, AsyncSessionLocal
from src.models.liberclaw_user import LiberclawUser  # noqa: F401 - must be imported for FK resolution

if TYPE_CHECKING:
    from src.models.user import User
    from src.models.inference_call import InferenceCall
    from src.models.chat_request import ChatRequest


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    user_address: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.address", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monthly_limit: Mapped[float | None] = mapped_column(Float, nullable=True)  # Credits limit per month
    type: Mapped[ApiKeyType] = mapped_column(Enum(ApiKeyType), nullable=False, default=ApiKeyType.api)
    liberclaw_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("liberclaw_users.id", ondelete="CASCADE"), nullable=True
    )

    user: Mapped["User"] = relationship("User", back_populates="api_keys")
    usages: Mapped[list["InferenceCall"]] = relationship(
        "InferenceCall", back_populates="api_key", cascade="all, delete-orphan"
    )
    chat_requests: Mapped[list["ChatRequest"]] = relationship(
        "ChatRequest", back_populates="api_key", cascade="all, delete-orphan"
    )
    liberclaw_user: Mapped["LiberclawUser | None"] = relationship("LiberclawUser", back_populates="api_keys")

    __table_args__ = (UniqueConstraint("user_address", "name", name="unique_api_key_name_per_user"),)

    def __init__(
        self,
        key: str,
        name: str,
        user_address: str | None = None,
        monthly_limit: float | None = None,
        type: ApiKeyType = ApiKeyType.api,
        liberclaw_user_id: uuid.UUID | None = None,
    ):
        self.key = key
        self.name = name
        self.user_address = user_address
        self.monthly_limit = monthly_limit
        self.type = type
        self.liberclaw_user_id = liberclaw_user_id

    @property
    def masked_key(self) -> str:
        if len(self.key) <= 8:
            return "****"
        return f"{self.key[:4]}...{self.key[-4:]}"

    @staticmethod
    def generate_key() -> str:
        return secrets.token_hex(16)

    async def get_current_month_usage(self) -> float:
        from src.models.inference_call import InferenceCall

        async with AsyncSessionLocal() as db:
            now = datetime.now()
            first_day = datetime(now.year, now.month, 1)
            next_month = datetime(now.year + (now.month // 12), ((now.month % 12) + 1), 1)

            result = await db.execute(
                select(sql_func.coalesce(sql_func.sum(InferenceCall.credits_used), 0.0)).where(
                    InferenceCall.api_key_id == self.id,
                    InferenceCall.used_at >= first_day,
                    InferenceCall.used_at < next_month,
                )
            )
            return float(result.scalar() or 0.0)

    async def get_effective_limit_remaining(self) -> float:
        if not self.user_address:
            return 0.0

        from src.services.credit import CreditService

        user_balance = await CreditService.get_balance(self.user_address)

        if self.monthly_limit is not None:
            usage = await self.get_current_month_usage()
            limit_remaining = max(0.0, self.monthly_limit - usage)
            return min(limit_remaining, user_balance)

        return user_balance
