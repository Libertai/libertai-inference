import uuid
from datetime import datetime, timedelta

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, status
from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.blockchain import LibertaiChain
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from src.config import config
from src.interfaces.api_keys import ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.users import get_or_create_user_by_wallet, get_user_by_id
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# API key types that are a user's own inference credentials, and so identify them.
USER_OWNED_KEY_TYPES = (ApiKeyType.api, ApiKeyType.cli)


def create_access_token(address: str, chain: LibertaiChain) -> str:
    """Create a JWT access token for the given wallet address and chain."""
    expire = datetime.now() + timedelta(minutes=config.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": address, "chain": chain.value, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, config.JWT_SECRET, algorithm="HS256")
    return encoded_jwt


def _extract_token(authorization: str | None, libertai_auth: str | None) -> str | None:
    """Prefer an Authorization: Bearer header, fall back to the legacy cookie."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return libertai_auth


def _reject_if_suspended(user: User) -> None:
    """Same 401 as an unknown token: the reason is not surfaced."""
    if user.suspended_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")


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
            _reject_if_suspended(user)
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
        _reject_if_suspended(user)
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


async def get_user_from_api_key(authorization: str | None = Header(default=None)) -> User:
    """Resolve the owner of an inference API key sent as ``Authorization: Bearer <key>``.

    Only a user's own inference credentials qualify (api, cli): chat keys belong to the
    apps, liberclaw/x402/pool to services. A key that cannot run inference cannot read
    usage either. Every rejection is the same 401 — which of these applies is not surfaced.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    key = authorization[7:].strip()
    now = datetime.now()
    async with AsyncSessionLocal() as db:
        api_key = (
            (
                await db.execute(
                    select(ApiKeyDB)
                    .options(joinedload(ApiKeyDB.user))
                    .where(
                        ApiKeyDB.key == key,
                        ApiKeyDB.type.in_(USER_OWNED_KEY_TYPES),
                        ApiKeyDB.deleted_at.is_(None),
                        ApiKeyDB.is_active == True,
                        or_(ApiKeyDB.expires_at.is_(None), ApiKeyDB.expires_at > now),
                    )
                )
            )
            .scalars()
            .first()
        )

    if api_key is None or api_key.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if api_key.user.suspended_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return api_key.user


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
