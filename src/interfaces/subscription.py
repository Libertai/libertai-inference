from enum import Enum

from pydantic import BaseModel


class SubscriptionProvider(str, Enum):
    hold = "hold"
    subs = "subs"
    vouchers = "vouchers"


class SubscriptionType(str, Enum):
    pro = "pro"
    advanced = "advanced"
    agent = "agent"


class SubscriptionChain(str, Enum):
    base = "base"
    solana = "solana"


class SubscriptionAccount(BaseModel):
    address: str
    chain: SubscriptionChain

    class Config:
        schema_extra = {"example": {"address": "0x0000000000000000000000000000000000000000", "chain": "base"}}


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
    multiple: bool = False
