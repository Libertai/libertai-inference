from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.subscription import SubscriptionType, SubscriptionAccount
from pydantic import BaseModel, validator

from src.config import config
from src.utils.general import get_current_time


class VouchersSubscription(BaseModel):
    account: SubscriptionAccount
    type: SubscriptionType
    end_time: int

    # noinspection PyMethodParameters
    @validator("end_time")
    def valid_end_time(cls, end_time):
        current_time = get_current_time()
        if end_time <= current_time:
            raise ValueError("end_time can't be in the past")
        return end_time

    # noinspection PyMethodParameters
    @validator("account")
    def format_address(cls, account: SubscriptionAccount):
        """Convert address to be able to compare it with others"""
        return SubscriptionAccount(address=format_address(account.address, account.chain), chain=account.chain)


class VouchersPasswordBody(BaseModel):
    password: str

    # noinspection PyMethodParameters
    @validator("password")
    def valid_password(cls, password):
        if password not in config.VOUCHERS_PASSWORDS:
            raise ValueError("Given password isn't in the list of allowed passwords.")


class VouchersPostSubscribeBody(VouchersPasswordBody):
    subscriptions: list[VouchersSubscription]


class VouchersDeleteSubscribeBody(VouchersPasswordBody):
    subscription_ids: list[str]


class VouchersCreatedSubscription(VouchersSubscription):
    post_hash: str
    subscription_id: str


class VouchersPostSubscriptionResponse(BaseModel):
    created_subscriptions: list[VouchersCreatedSubscription]


class VouchersPostRefreshSubscriptionsResponse(BaseModel):
    cancelled_subscriptions: list[str]


class VouchersDeleteSubscriptionResponse(VouchersPostRefreshSubscriptionsResponse):
    not_found_subscriptions: list[str]
