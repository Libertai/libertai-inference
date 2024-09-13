from enum import Enum

from pydantic import BaseModel


class SubscriptionProvider(str, Enum):
    hold = 'hold'


class SubscriptionType(str, Enum):
    basic = 'basic'


class Subscription(BaseModel):
    id: str
    provider: SubscriptionProvider
    type: SubscriptionType
    started_at: int
    ended_at: int | None
    is_active: bool
