import enum
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, field_serializer


class AlephGrantStatus(str, enum.Enum):
    none = "none"  # No wallet given yet
    pending = "pending"  # Signed and stored; submitted or waiting for a retry
    sent = "sent"  # Processed by the Aleph network
    failed = "failed"  # Rejected or retries exhausted: needs a human


def _utc_iso(value: datetime | None) -> str | None:
    """Naive UTC timestamps from the DB, rendered with an explicit Z for the frontends."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


class CampaignEvent(BaseModel):
    name: str
    starts_at: datetime
    ends_at: datetime

    @field_serializer("starts_at", "ends_at")
    def _serialize_dt(self, value: datetime) -> str | None:
        return _utc_iso(value)


class CampaignPublicResponse(BaseModel):
    code: str
    libertai_amount: float
    aleph_amount: float
    credit_validity_days: int
    status: Literal["open", "upcoming", "closed", "full"]
    event: CampaignEvent | None


class CampaignClaimRequest(BaseModel):
    aleph_address: str | None = None


class CampaignAttachAlephRequest(BaseModel):
    aleph_address: str


class CampaignClaimResponse(BaseModel):
    code: str
    event_name: str
    created_at: datetime
    libertai_amount: float
    libertai_expires_at: datetime
    aleph_amount: float
    aleph_address: str | None
    aleph_status: AlephGrantStatus
    aleph_item_hash: str | None
    aleph_expires_at: datetime | None

    @field_serializer("created_at", "libertai_expires_at", "aleph_expires_at")
    def _serialize_dt(self, value: datetime | None) -> str | None:
        return _utc_iso(value)
