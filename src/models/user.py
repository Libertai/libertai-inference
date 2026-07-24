import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import TIMESTAMP, UUID, Boolean, Float, String, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import AsyncSessionLocal, Base

if TYPE_CHECKING:
    from src.models.api_key import ApiKey
    from src.models.credit_transaction import CreditTransaction
    from src.models.oauth_connection import OAuthConnection
    from src.models.plan_subscription import PlanSubscription
    from src.models.session import Session
    from src.models.wallet_connection import WalletConnection


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=func.current_timestamp())
    # Grants access to the staff backoffice (analytics + admin actions). Set manually via SQL.
    is_libertai_staff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Monthly cap (USD credits) on overflow spend beyond entitlement windows. NULL = unlimited.
    monthly_extra_credit_cap: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Legacy wallet address. Identity moved to wallet_connections; kept (nullable, unique) for one
    # release as a rollback hatch and so existing address-based FKs resolve until the FK swap.
    address: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)

    credit_transactions: Mapped[list["CreditTransaction"]] = relationship(
        "CreditTransaction", back_populates="user", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list["ApiKey"]] = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")
    wallet_connections: Mapped[list["WalletConnection"]] = relationship(
        "WalletConnection", back_populates="user", cascade="all, delete-orphan"
    )
    oauth_connections: Mapped[list["OAuthConnection"]] = relationship(
        "OAuthConnection", back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["Session"]] = relationship(
        "Session", back_populates="user", cascade="all, delete-orphan"
    )
    plan_subscriptions: Mapped[list["PlanSubscription"]] = relationship(
        "PlanSubscription", back_populates="user", cascade="all, delete-orphan"
    )

    def __init__(
        self,
        address: str | None = None,
        email: str | None = None,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ):
        self.address = address
        self.email = email
        self.display_name = display_name
        self.avatar_url = avatar_url

    async def get_credit_balance(self) -> float:
        from src.models.credit_transaction import CreditTransaction, CreditTransactionStatus

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.coalesce(func.sum(CreditTransaction.amount_left), 0.0)).where(
                    CreditTransaction.user_id == self.id,
                    CreditTransaction.is_active == True,
                    CreditTransaction.status == CreditTransactionStatus.completed,
                )
            )
            return float(result.scalar() or 0.0)
