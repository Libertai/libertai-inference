import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    TIMESTAMP,
    UUID,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.api_key import ApiKey


class ChatRequest(Base):
    __tablename__ = "chat_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_key_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    model_name: Mapped[str] = mapped_column(String, nullable=False)

    api_key: Mapped["ApiKey"] = relationship("ApiKey", back_populates="chat_requests")

    def __init__(
        self,
        api_key_id: uuid.UUID,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        model_name: str,
    ):
        self.api_key_id = api_key_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.model_name = model_name
