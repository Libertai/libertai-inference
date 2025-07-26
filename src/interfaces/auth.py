from libertai_utils.chains.index import is_address_valid
from libertai_utils.interfaces.blockchain import LibertaiChain
from pydantic import BaseModel, field_validator
from pydantic_core.core_schema import FieldValidationInfo


class AuthMessageRequest(BaseModel):
    chain: LibertaiChain
    address: str

    @field_validator("address")
    def validate_address(cls, value, info: FieldValidationInfo):
        chain: LibertaiChain = info.data.get("chain")
        if not is_address_valid(chain, value):
            raise ValueError(f"Invalid address for chain {chain}")
        return value


class AuthMessageResponse(BaseModel):
    message: str


class AuthLoginRequest(BaseModel):
    chain: LibertaiChain
    address: str
    signature: str

    @field_validator("address")
    def validate_address(cls, value, info: FieldValidationInfo):
        chain: LibertaiChain = info.data.get("chain")
        if not is_address_valid(chain, value):
            raise ValueError(f"Invalid address for chain {chain}")
        return value


class AuthLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    address: str


class AuthStatusResponse(BaseModel):
    authenticated: bool
    address: str | None = None
