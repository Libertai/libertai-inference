import uuid

from sqlalchemy import UUID, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LiberclawBillingDetails(Base):
    """Optional buyer identity printed on LCLW invoices. Free text (no VIES validation);
    snapshotted onto each invoice at issue time — edits never retro-apply.

    Keyed on liberclaw_account_id, which lives in LiberClaw's own database: no FK here."""

    __tablename__ = "liberclaw_billing_details"

    liberclaw_account_id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vat_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    def __init__(
        self,
        liberclaw_account_id: uuid.UUID,
        name: str | None = None,
        address_line1: str | None = None,
        address_line2: str | None = None,
        postal_code: str | None = None,
        city: str | None = None,
        country: str | None = None,
        vat_number: str | None = None,
    ):
        self.liberclaw_account_id = liberclaw_account_id
        self.name = name
        self.address_line1 = address_line1
        self.address_line2 = address_line2
        self.postal_code = postal_code
        self.city = city
        self.country = country
        self.vat_number = vat_number

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
