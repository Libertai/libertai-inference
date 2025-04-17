from pydantic import BaseModel


class AuthMessageRequest(BaseModel):
    address: str


class AuthMessageResponse(BaseModel):
    message: str


class AuthLoginRequest(BaseModel):
    address: str
    signature: str


class AuthLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    address: str


class AuthStatusResponse(BaseModel):
    authenticated: bool
    address: str | None = None
