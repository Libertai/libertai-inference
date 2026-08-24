import re

from pydantic import BaseModel, Field, field_validator

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
