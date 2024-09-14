from pydantic import validator
from pydantic.main import BaseModel

from src.interfaces.subscription import SubscriptionType, SubscriptionAccount
from src.utils.ethereum import get_address_from_signature


class HoldPostSubscribeBody(BaseModel):
    type: SubscriptionType
    account: SubscriptionAccount
    signature: str

    # noinspection PyMethodParameters
    @validator("signature")
    def valid_signature(cls, signature, values):
        if "account" in values:
            address = get_address_from_signature("Placeholder", signature)
            if address.upper() != values["account"].address.upper():
                raise ValueError("Signature doesn't match the address in account.address")
        return signature


class HoldAggregateData(BaseModel):
    tokens: dict[str, int]
