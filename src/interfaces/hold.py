from pydantic import validator, root_validator
from pydantic.main import BaseModel

from src.interfaces.subscription import SubscriptionType, SubscriptionAccount, SubscriptionProvider, SubscriptionChain
from src.utils.blockchains.ethereum import format_eth_address
from src.utils.blockchains.index import is_signature_valid
from src.utils.signature import get_subscribe_message, get_unsubscribe_message


class BaseHoldSubscriptionBody(BaseModel):
    account: SubscriptionAccount
    signature: str
    type: SubscriptionType

    # noinspection PyMethodParameters
    @validator("account")
    def format_address(cls, account: SubscriptionAccount):
        """Convert address to be able to compare it with others"""
        if account.chain == SubscriptionChain.base:
            return SubscriptionAccount(address=format_eth_address(account.address), chain=account.chain)
        return account


class HoldPostSubscriptionBody(BaseHoldSubscriptionBody):
    # noinspection PyMethodParameters
    @root_validator
    def valid_signature(cls, values):
        """Check if the signature is valid"""
        valid = is_signature_valid(
            values["account"].chain,
            get_subscribe_message(values["type"], SubscriptionProvider.hold),
            values["signature"],
            values["account"].address,
        )

        if not valid:
            raise ValueError("Signature doesn't match the address in account.address")
        return values


class HoldDeleteSubscriptionBody(BaseHoldSubscriptionBody):
    subscription_id: str

    # noinspection PyMethodParameters
    @root_validator
    def valid_signature(cls, values):
        """Check if the signature is valid"""
        valid = is_signature_valid(
            values["account"].chain,
            get_unsubscribe_message(values["type"], SubscriptionProvider.hold),
            values["signature"],
            values["account"].address,
        )

        if not valid:
            raise ValueError("Signature doesn't match the address in account.address")
        return values


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
