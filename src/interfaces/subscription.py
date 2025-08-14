import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from src.models.subscription import SubscriptionType, SubscriptionStatus
from src.models.subscription_transaction import SubscriptionTransactionStatus


class SubscriptionResponse(BaseModel):
    id: uuid.UUID
    user_address: str
    subscription_type: SubscriptionType
    amount: float
    last_charged_at: datetime
    next_charge_at: datetime
    status: SubscriptionStatus
    created_at: datetime
    related_id: uuid.UUID


class SubscriptionTransactionResponse(BaseModel):
    id: uuid.UUID
    subscription_id: uuid.UUID
    amount: float
    status: SubscriptionTransactionStatus
    created_at: datetime
    notes: Optional[str] = None
