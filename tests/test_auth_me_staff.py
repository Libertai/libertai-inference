from src.interfaces.auth import CurrentUserResponse
from src.models.base import AsyncSessionLocal
from src.models.user import User


async def test_user_defaults_to_non_staff():
    async with AsyncSessionLocal() as db:
        user = User(email="staff-default@example.com")
        db.add(user)
        await db.commit()
        assert user.is_libertai_staff is False


async def test_current_user_response_exposes_staff_flag():
    resp = CurrentUserResponse(id="x", is_libertai_staff=True)
    assert resp.is_libertai_staff is True
