from enum import Enum

from pydantic import BaseModel


class SubscriptionProvider(str, Enum):
    hold = "hold"
    subs = "subs"


class SubscriptionType(str, Enum):
    standard = "standard"


class SubscriptionChain(str, Enum):
    base = "base"


class SubscriptionAccount(BaseModel):
    address: str
    chain: SubscriptionChain


class BaseSubscription(BaseModel):
    id: str
    type: SubscriptionType
    provider: SubscriptionProvider
    started_at: int
    ended_at: int | None
    is_active: bool


class Subscription(BaseSubscription):
    provider_data: dict
    account: SubscriptionAccount
    tags: list[str]


class FetchedSubscription(Subscription):
    post_hash: str


class GetUserSubscriptionsResponse(BaseModel):
    subscriptions: list[BaseSubscription]


class SubscriptionDefinition(BaseModel):
    type: SubscriptionType
    providers: list[SubscriptionProvider]
    multiple: bool
