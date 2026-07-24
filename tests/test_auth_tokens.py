import uuid

import jwt
import pytest
from freezegun import freeze_time

from src.services.auth_tokens import (
    ACCESS,
    REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
)


def test_access_token_roundtrip():
    user_id = uuid.uuid4()
    payload = decode_token(create_access_token(user_id), ACCESS)
    assert payload["sub"] == str(user_id)
    assert payload["type"] == ACCESS


def test_refresh_token_carries_session():
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    payload = decode_token(create_refresh_token(user_id, session_id), REFRESH)
    assert payload["sub"] == str(user_id)
    assert payload["sid"] == str(session_id)
    assert payload["type"] == REFRESH


def test_wrong_type_rejected():
    token = create_access_token(uuid.uuid4())
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, REFRESH)


def test_expired_access_token_rejected():
    with freeze_time("2026-01-01 00:00:00"):
        token = create_access_token(uuid.uuid4())
    with freeze_time("2026-12-31 00:00:00"), pytest.raises(jwt.ExpiredSignatureError):
        decode_token(token, ACCESS)
