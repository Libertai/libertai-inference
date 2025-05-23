from datetime import datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status, Cookie, Header
from pydantic import BaseModel

from src.config import config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class TokenData(BaseModel):
    address: str


def create_access_token(address: str) -> str:
    """Create a JWT access token for the given wallet address."""
    expire = datetime.now() + timedelta(minutes=config.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": address, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, config.JWT_SECRET, algorithm="HS256")
    return encoded_jwt


def verify_token(libertai_auth: str = Cookie(default=None)) -> TokenData:
    """Verify JWT token from cookie and return the wallet address."""
    if not libertai_auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    try:
        # Added options to explicitly verify expiration of tokens
        payload = jwt.decode(libertai_auth, config.JWT_SECRET, algorithms=["HS256"], options={"verify_exp": True})
        address: str | None = payload.get("sub")
        if address is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
            )
        token_data = TokenData(address=address)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired",
        )
    except jwt.PyJWTError as e:
        logger.error(f"JWT verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    return token_data


def get_current_address(token_data: Annotated[TokenData, Depends(verify_token)]) -> str:
    """Return the current wallet address from the token."""
    return token_data.address


def verify_admin_token(x_admin_token: str = Header(...)) -> None:
    """Verify the admin token from header."""
    if not config.ADMIN_SECRET:
        logger.error("ADMIN_SECRET not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin authentication not configured",
        )

    if x_admin_token != config.ADMIN_SECRET:
        logger.warning("Invalid admin token attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
        )

    # If we got here, the token is valid
