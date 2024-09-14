from enum import Enum

from pydantic import BaseModel


class SubscriptionProvider(str, Enum):
    hold = "hold"


class SubscriptionType(str, Enum):
    basic = "basic"


class SubscriptionChain(str, Enum):
    base = "base"


class SubscriptionAccount(BaseModel):
    address: str
    chain: SubscriptionChain


class Subscription(BaseModel):
    id: str
    type: SubscriptionType
    provider: SubscriptionProvider
    account: SubscriptionAccount
    started_at: int
    ended_at: int | None
    is_active: bool
    tags: list[str]


class SubscriptionDefinition(BaseModel):
    type: SubscriptionType
    providers: list[SubscriptionProvider]
    multiple: bool


type SubscriptionGroup = list[SubscriptionDefinition]
