from pydantic import validator, root_validator
from pydantic.main import BaseModel

from src.interfaces.subscription import SubscriptionType, SubscriptionAccount, SubscriptionProvider
from src.utils.ethereum import get_address_from_signature, format_eth_address
from src.utils.signature import get_subscribe_message, get_unsubscribe_message


class BaseHoldSubscriptionBody(BaseModel):
    account: SubscriptionAccount
    signature: str
    type: SubscriptionType

    # noinspection PyMethodParameters
    @validator("account")
    def format_address(cls, account: SubscriptionAccount):
        # Convert address to be able to compare it with others
        return SubscriptionAccount(address=format_eth_address(account.address), chain=account.chain)


class HoldPostSubscriptionBody(BaseHoldSubscriptionBody):
    # noinspection PyMethodParameters
    @root_validator
    def valid_signature(cls, values):
        address = get_address_from_signature(
            get_subscribe_message(values["type"], SubscriptionProvider.hold), values["signature"]
        )
        if format_eth_address(address) != format_eth_address(values["account"].address):
            raise ValueError("Signature doesn't match the address in account.address")
        return values


class HoldDeleteSubscriptionBody(BaseHoldSubscriptionBody):
    subscription_id: str

    # noinspection PyMethodParameters
    @validator("signature")
    def valid_signature(cls, signature, values):
        address = get_address_from_signature(
            get_unsubscribe_message(values["type"], SubscriptionProvider.hold), signature
        )
        if format_eth_address(address) != format_eth_address(values["account"].address):
            raise ValueError("Signature doesn't match the address in account.address")


class HoldPostSubscriptionResponse(BaseModel):
    post_hash: str
    subscription_id: str


class HoldDeleteSubscriptionResponse(BaseModel):
    success: bool


class HoldPostRefreshSubscriptionsResponse(BaseModel):
    cancelled_subscriptions: list[str]


class HoldAggregateData(BaseModel):
    tokens: dict[str, int]


class HoldGetMessagesResponse(BaseModel):
    subscribe_message: str
    unsubscribe_message: str
