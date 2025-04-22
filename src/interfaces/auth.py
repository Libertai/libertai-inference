from pydantic import BaseModel, field_validator
from web3 import Web3


class AuthMessageRequest(BaseModel):
    address: str

    @field_validator("address")
    def validate_eth_address(cls, value):
        return Web3.to_checksum_address(value)


class AuthMessageResponse(BaseModel):
    message: str


class AuthLoginRequest(BaseModel):
    address: str
    signature: str

    @field_validator("address")
    def validate_eth_address(cls, value):
        return Web3.to_checksum_address(value)


class AuthLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    address: str


class AuthStatusResponse(BaseModel):
    authenticated: bool
    address: str | None = None
