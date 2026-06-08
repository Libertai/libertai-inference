"""Profile display-name update via the users service."""

import pytest

from src.interfaces.auth import UpdateProfileRequest
from src.models.base import AsyncSessionLocal
from src.services.users import get_or_create_user_by_email, update_user_profile


async def _make_user(email: str):
    async with AsyncSessionLocal() as db:
        user, _ = await get_or_create_user_by_email(db, email)
        await db.commit()
        return user.id


async def test_update_display_name_persists():
    user_id = await _make_user("name-update@example.com")

    async with AsyncSessionLocal() as db:
        updated = await update_user_profile(db, user_id, "Reza")
        await db.commit()
        assert updated.display_name == "Reza"

    async with AsyncSessionLocal() as db:
        from src.models.user import User

        assert (await db.get(User, user_id)).display_name == "Reza"


async def test_empty_display_name_normalizes_to_none():
    # The request model trims and turns blank into None (clears the name).
    assert UpdateProfileRequest(display_name="   ").display_name is None
    assert UpdateProfileRequest(display_name="  Bob  ").display_name == "Bob"


def test_display_name_length_capped():
    with pytest.raises(ValueError):
        UpdateProfileRequest(display_name="x" * 51)
