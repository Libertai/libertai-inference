from datetime import datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from src.config import config
from src.interfaces.auth import AuthJWTSettings
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


jwt_settings = AuthJWTSettings(secret=config.JWT_SECRET, expire_minutes=config.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
security = HTTPBearer(description="JWT token for authentication")


class TokenData(BaseModel):
    address: str


def create_access_token(address: str) -> str:
    """Create a JWT access token for the given wallet address."""
    expire = datetime.now() + timedelta(minutes=jwt_settings.expire_minutes)
    to_encode = {"sub": address, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, jwt_settings.secret, algorithm=jwt_settings.algorithm)
    return encoded_jwt


def verify_token(credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)]) -> TokenData:
    """Verify JWT token and return the wallet address."""
    try:
        payload = jwt.decode(credentials.credentials, jwt_settings.secret, algorithms=[jwt_settings.algorithm])
        address: str | None = payload.get("sub")
        if address is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token_data = TokenData(address=address)
    except jwt.PyJWTError as e:
        logger.error(f"JWT verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token_data


def get_current_address(token_data: Annotated[TokenData, Depends(verify_token)]) -> str:
    """Return the current wallet address from the token."""
    return token_data.address
