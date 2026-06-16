import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, UUID, Boolean, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.interfaces.device_tokens import DevicePlatform
from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    token: Mapped[str] = mapped_column(String(4096), unique=True, nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[DevicePlatform] = mapped_column(Enum(DevicePlatform), nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, default=func.current_timestamp(), onupdate=func.current_timestamp()
    )

    user: Mapped["User"] = relationship("User", back_populates="device_tokens")

    def __init__(
        self,
        token: str,
        user_id: uuid.UUID,
        platform: DevicePlatform,
        app_version: str | None = None,
        enabled: bool = True,
    ):
        self.token = token
        self.user_id = user_id
        self.platform = platform
        self.app_version = app_version
        self.enabled = enabled
