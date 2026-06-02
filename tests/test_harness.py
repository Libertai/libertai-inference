"""Smoke tests for the pytest harness itself."""

from sqlalchemy import text


async def test_db_fixture(db):
    assert db is not None


async def test_db_is_connected(db):
    result = await db.execute(text("SELECT 1"))
    assert result.scalar() == 1
