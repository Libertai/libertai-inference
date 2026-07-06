import pytest
from fastapi import HTTPException

from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.services.auth import get_current_user, require_staff
from src.services.auth_tokens import create_access_token


async def _make_user(email: str, staff: bool) -> User:
    async with AsyncSessionLocal() as db:
        user = User(email=email)
        user.is_libertai_staff = staff
        db.add(user)
        await db.commit()
        return user


async def test_staff_user_passes():
    user = await _make_user("staff@example.com", staff=True)
    resolved = await get_current_user(authorization=f"Bearer {create_access_token(user.id)}")
    assert (await require_staff(resolved)).id == user.id


async def test_non_staff_user_gets_403():
    user = await _make_user("pleb@example.com", staff=False)
    resolved = await get_current_user(authorization=f"Bearer {create_access_token(user.id)}")
    with pytest.raises(HTTPException) as exc:
        await require_staff(resolved)
    assert exc.value.status_code == 403


async def test_missing_token_still_401():
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=None, libertai_auth=None)
    assert exc.value.status_code == 401
