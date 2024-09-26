from enum import Enum

from pydantic import BaseModel


class SubscriptionSubsProviderData(BaseModel):
    subsId: str
    tokenAddress: str


class SubsChain(str, Enum):
    bsc = "bsc"


class SubsConfig(BaseModel):
    api_url: str
    api_key: str
    chain: SubsChain
    chain_rpc: str
    app_id: int


class SubsPostRefreshSubscriptionsResponse(BaseModel):
    created_subscriptions: list[str]
    cancelled_subscriptions: list[str]


class SubsAPIGetSubscriptionsResponse(BaseModel):
    subsId: str
    payeeAddress: str
    tokenAddress: str
    PaymentName: str
    paymentId: str
    startTime: int
    active: bool
    nextPaymentTime: int
    renewTime: int
