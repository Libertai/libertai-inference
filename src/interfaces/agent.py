from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from src.models import SubscriptionStatus


class RenewTransaction(BaseModel):
    amount: float
    timestamp: datetime


class CreateAgentRequest(BaseModel):
    name: str
    ssh_public_key: str
    subscription_months: int = 1  # Subscription period in months


class AgentResponse(BaseModel):
    id: UUID
    instance_hash: str | None
    name: str
    user_address: str
    created_at: datetime
    monthly_cost: float
    paid_until: datetime
    renew_history: list[dict[str, Any]]
    subscription_status: SubscriptionStatus
    subscription_id: UUID | None = None


class GetAgentResponse(BaseModel):
    id: UUID
    instance_hash: str | None
    name: str
    user_address: str
    monthly_cost: float
    paid_until: datetime
    instance_ip: str | None = None
    subscription_status: SubscriptionStatus
    subscription_id: UUID | None = None


class UploadAgentCodeRequest(BaseModel):
    code_url: str
    python_version: str = "3.11"


class UpdateAgentResponse(BaseModel):
    instance_ip: str | None = None
    error_log: str | None = None


class AddSSHKeyRequest(BaseModel):
    ssh_key: str


class AddSSHKeyResponse(BaseModel):
    error_log: str | None = None


class ResubscribeAgentRequest(BaseModel):
    """Request to resubscribe a deactivated agent"""

    subscription_months: int = 1  # Subscription period in months


class ResubscribeAgentResponse(BaseModel):
    """Response for agent resubscription"""

    success: bool
    paid_until: datetime | None = None
    error: str | None = None
