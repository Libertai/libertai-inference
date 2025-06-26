from pydantic import BaseModel, field_validator

from src.utils.address import validate_and_format_address


class AuthMessageRequest(BaseModel):
    address: str

    @field_validator("address")
    def validate_address(cls, value):
        return validate_and_format_address(value)


class AuthMessageResponse(BaseModel):
    message: str


class AuthLoginRequest(BaseModel):
    address: str
    signature: str
    chain: str

    @field_validator("address")
    def validate_address(cls, value):
        return validate_and_format_address(value)


class AuthLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    address: str


class AuthStatusResponse(BaseModel):
    authenticated: bool
    address: str | None = None
