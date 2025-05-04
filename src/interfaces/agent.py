from pydantic import BaseModel


class PublicAgentData(BaseModel):
    id: str
    subscription_id: str
    instance_hash: str
    last_update: int


class Agent(PublicAgentData):
    encrypted_secret: str
    encrypted_ssh_key: str
    tags: list[str]


class FetchedAgent(Agent):
    post_hash: str


class GetAgentSecretMessage(BaseModel):
    message: str


class GetAgentResponse(PublicAgentData):
    instance_ip: str | None


class GetAgentSecretResponse(BaseModel):
    secret: str
