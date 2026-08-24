import re
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from src.interfaces.common import UtcDatetime

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class BillingDetailsUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    postal_code: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=128)
    country: str | None = Field(default=None, max_length=64)
    vat_number: str | None = Field(default=None, max_length=32)

    @field_validator("*", mode="before")
    @classmethod
    def _clean(cls, v):
        if isinstance(v, str):
            v = _CONTROL_CHARS.sub("", v).strip()
            return v or None
        return v


class BillingDetailsResponse(BillingDetailsUpdate):
    pass


class InvoiceResponse(BaseModel):
    id: uuid.UUID
    number: str
    issued_at: UtcDatetime
    payment_date: UtcDatetime
    currency: str
    # Decimal serializes as an exact string in JSON: this is a billing API, amounts never
    # pass through float.
    net_amount: Decimal
    vat_amount: Decimal
    gross_amount: Decimal
    line_label: str
    period_start: UtcDatetime | None
    period_end: UtcDatetime | None


class InvoiceListResponse(BaseModel):
    items: list[InvoiceResponse]
    total: int
