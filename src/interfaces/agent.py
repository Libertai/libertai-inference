from pydantic import BaseModel

from src.interfaces.subscription import SubscriptionAccount


class DeleteAgentBody(BaseModel):
    subscription_id: str
    password: str


class SetupAgentBody(DeleteAgentBody):
    account: SubscriptionAccount
