"""UUID-based access + refresh JWTs (pyjwt, HS256).

Distinct from the legacy wallet token in services/auth.py (sub=address): here
sub is the user UUID. Refresh tokens also carry `sid` (the Session id) so they
can be rotated/revoked.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from src.config import config

ACCESS = "access"
REFRESH = "refresh"


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def create_access_token(user_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    return _encode(
        {
            "sub": str(user_id),
            "type": ACCESS,
            "iat": now,
            "exp": now + timedelta(minutes=config.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
        }
    )


def create_refresh_token(user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    return _encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "type": REFRESH,
            "jti": str(uuid.uuid4()),  # unique per token so rotation always yields a distinct token
            "iat": now,
            "exp": now + timedelta(days=config.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
        }
    )


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    """Decode + validate a token, enforcing its `type`. Raises jwt.PyJWTError on failure."""
    payload: dict[str, Any] = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"Expected {expected_type} token, got {payload.get('type')}")
    return payload
