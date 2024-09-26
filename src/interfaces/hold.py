from pydantic import validator
from pydantic.main import BaseModel

from src.interfaces.subscription import SubscriptionType, SubscriptionAccount
from src.utils.ethereum import get_address_from_signature, format_eth_address


class BaseHoldSubscriptionBody(BaseModel):
    account: SubscriptionAccount
    signature: str

    # noinspection PyMethodParameters
    @validator("signature")
    def valid_signature(cls, signature, values):
        if "account" in values:
            # TODO: change this message, and maybe move validation elsewhere
            address = get_address_from_signature("Placeholder", signature)
            if format_eth_address(address) != format_eth_address(values["account"].address):
                raise ValueError("Signature doesn't match the address in account.address")
        return signature

    # noinspection PyMethodParameters
    @validator("account")
    def format_address(cls, account: SubscriptionAccount):
        # Convert address to be able to compare it with others
        return SubscriptionAccount(address=format_eth_address(account.address), chain=account.chain)


class HoldPostSubscriptionBody(BaseHoldSubscriptionBody):
    type: SubscriptionType


class HoldDeleteSubscriptionBody(BaseHoldSubscriptionBody):
    subscription_id: str


class HoldPostSubscriptionResponse(BaseModel):
    post_hash: str
    subscription_id: str


class HoldDeleteSubscriptionResponse(BaseModel):
    success: bool


class HoldPostRefreshSubscriptionsResponse(BaseModel):
    cancelled_subscriptions: list[str]


class HoldAggregateData(BaseModel):
    tokens: dict[str, int]
