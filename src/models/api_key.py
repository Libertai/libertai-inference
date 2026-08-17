import secrets
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, UUID, Boolean, Enum, Float, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.interfaces.api_keys import ApiKeyType
from src.models.base import Base
from src.models.liberclaw_user import LiberclawUser

if TYPE_CHECKING:
    from src.models.chat_request import ChatRequest
    from src.models.inference_call import InferenceCall
    from src.models.user import User


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        # One live CLI key per (user, device name). Scoped to cli — name uniqueness is
        # deliberately absent for type=api. Soft-deleted rows fall outside the index, so a
        # disconnect followed by `libertai login` mints a fresh row rather than colliding.
        Index(
            "uq_api_keys_cli_user_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("type = 'cli' AND deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    # Legacy wallet address kept (no FK) for one release as a rollback hatch; identity is user_id.
    user_address: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Soft delete: set instead of removing the row, so related inference_calls (usage
    # history) are preserved. A deleted key is hidden from the user and unusable.
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    monthly_limit: Mapped[float | None] = mapped_column(Float, nullable=True)  # Credits limit per month
    # Optional expiry. Used by CLI keys (type=cli) which must be re-minted via `libertai login`
    # once expired; null means the key never expires (standard keys).
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
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

    # Names are not unique in general: a user may have several keys sharing a name (and
    # reuse a soft-deleted key's name freely). Live CLI keys are the exception — see the
    # partial unique index in __table_args__.

    def __init__(
        self,
        key: str,
        name: str,
        user_id: uuid.UUID | None = None,
        user_address: str | None = None,
        monthly_limit: float | None = None,
        type: ApiKeyType = ApiKeyType.api,
        liberclaw_user_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
    ):
        self.key = key
        self.name = name
        self.user_id = user_id
        self.user_address = user_address
        self.monthly_limit = monthly_limit
        self.type = type
        self.liberclaw_user_id = liberclaw_user_id
        self.expires_at = expires_at

    @property
    def masked_key(self) -> str:
        if len(self.key) <= 8:
            return "****"
        return f"{self.key[:4]}...{self.key[-4:]}"

    @staticmethod
    def generate_key() -> str:
        return secrets.token_hex(16)
