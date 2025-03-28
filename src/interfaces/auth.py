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


class AuthJWTSettings(BaseModel):
    secret: str
    algorithm: str = "HS256"
    expire_minutes: int = 60 * 24 * 30  # 30 days
