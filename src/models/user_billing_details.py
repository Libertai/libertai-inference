import uuid
from typing import TYPE_CHECKING

from sqlalchemy import UUID, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base

if TYPE_CHECKING:
    from src.models.user import User


class UserBillingDetails(Base):
    """Optional buyer identity printed on invoices. Free text (no VIES validation);
    snapshotted onto each invoice at issue time — edits never retro-apply."""

    __tablename__ = "user_billing_details"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vat_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    def __init__(
        self,
        user_id: uuid.UUID,
        name: str | None = None,
        address_line1: str | None = None,
        address_line2: str | None = None,
        postal_code: str | None = None,
        city: str | None = None,
        country: str | None = None,
        vat_number: str | None = None,
    ):
        self.user_id = user_id
        self.name = name
        self.address_line1 = address_line1
        self.address_line2 = address_line2
        self.postal_code = postal_code
        self.city = city
        self.country = country
        self.vat_number = vat_number

    user: Mapped["User"] = relationship("User", back_populates="billing_details")

    def as_snapshot(self) -> dict:
        return {
            "name": self.name,
            "address_line1": self.address_line1,
            "address_line2": self.address_line2,
            "postal_code": self.postal_code,
            "city": self.city,
            "country": self.country,
            "vat_number": self.vat_number,
        }
