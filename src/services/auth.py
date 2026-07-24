import uuid
from datetime import datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, status
from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.blockchain import LibertaiChain
from pydantic import BaseModel

from src.config import config
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.users import get_or_create_user_by_wallet, get_user_by_id
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class TokenData(BaseModel):
    address: str
    chain: LibertaiChain


def create_access_token(address: str, chain: LibertaiChain) -> str:
    """Create a JWT access token for the given wallet address and chain."""
    expire = datetime.now() + timedelta(minutes=config.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": address, "chain": chain.value, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, config.JWT_SECRET, algorithm="HS256")
    return encoded_jwt


def verify_token(libertai_auth: str = Cookie(default=None)) -> TokenData:
    """Verify JWT token from cookie and return the wallet address and chain."""
    if not libertai_auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    try:
        # Added options to explicitly verify expiration of tokens
        payload = jwt.decode(libertai_auth, config.JWT_SECRET, algorithms=["HS256"], options={"verify_exp": True})
        address: str | None = payload.get("sub")
        chain_value: str | None = payload.get("chain")

        if address is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
            )

        # TODO: remove backward compatibility here
        # Handle backward compatibility for tokens without chain
        if chain_value is None:
            # Default to Base for old tokens
            chain = LibertaiChain.base
        else:
            try:
                chain = LibertaiChain(chain_value)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid chain in token",
                )

        token_data = TokenData(address=address, chain=chain)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired",
        )
    except jwt.PyJWTError as e:
        logger.error(f"JWT verification error: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    return token_data


def get_current_address(token_data: Annotated[TokenData, Depends(verify_token)]) -> str:
    """Return the current wallet address from the token, formatted according to the chain."""
    return format_address(token_data.chain, token_data.address)


def _extract_token(authorization: str | None, libertai_auth: str | None) -> str | None:
    """Prefer an Authorization: Bearer header, fall back to the legacy cookie."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return libertai_auth


async def _resolve_user_from_token(token: str) -> User:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication token has expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")

    # New UUID-based token: sub is the user id.
    try:
        user_id: uuid.UUID | None = uuid.UUID(sub)
    except (ValueError, AttributeError):
        user_id = None

    async with AsyncSessionLocal() as db:
        if user_id is not None:
            user = await get_user_by_id(db, user_id)
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials"
                )
            return user

        # Legacy wallet token: sub is an address (+ optional chain claim). Resolve to its user,
        # keeping pre-cutover console sessions alive.
        chain_value = payload.get("chain")
        try:
            chain = LibertaiChain(chain_value) if chain_value else LibertaiChain.base
        except ValueError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid chain in token")
        user = await get_or_create_user_by_wallet(db, format_address(chain, sub))
        await db.commit()
        return user


async def get_current_user(
    authorization: str | None = Header(default=None),
    libertai_auth: str | None = Cookie(default=None),
) -> User:
    """Resolve the authenticated user from a Bearer/cookie JWT (UUID or legacy wallet token)."""
    token = _extract_token(authorization, libertai_auth)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return await _resolve_user_from_token(token)


async def require_staff(user: User = Depends(get_current_user)) -> User:
    """Allow only LibertAI staff (backoffice endpoints)."""
    if not user.is_libertai_staff:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff access required")
    return user


async def get_optional_user(
    authorization: str | None = Header(default=None),
    libertai_auth: str | None = Cookie(default=None),
) -> User | None:
    token = _extract_token(authorization, libertai_auth)
    if not token:
        return None
    try:
        return await _resolve_user_from_token(token)
    except HTTPException:
        return None


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


def verify_liberclaw_token(x_liberclaw_token: str = Header(...)) -> None:
    """Verify the Liberclaw token from header."""
    if not config.LIBERCLAW_SECRET:
        logger.error("LIBERCLAW_SECRET not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Liberclaw authentication not configured",
        )

    if x_liberclaw_token != config.LIBERCLAW_SECRET:
        logger.warning("Invalid Liberclaw token attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Liberclaw credentials",
        )
