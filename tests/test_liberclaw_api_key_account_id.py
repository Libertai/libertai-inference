"""Liberclaw api-key sync: liberclaw_account_id is the identity bridge to invoices.

Exercises LiberclawService.get_or_create_api_key against the committed DB (it
opens its own session), so each test cleans up its own rows.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.liberclaw_user import LiberclawUser
from src.services.liberclaw import LiberclawService

pytestmark = pytest.mark.asyncio


async def _cleanup(lc_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawUser).where(LiberclawUser.id == lc_id))
        await db.commit()


async def _stored_account_id(lc_id):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(LiberclawUser.liberclaw_account_id).where(LiberclawUser.id == lc_id))).scalar()


async def test_account_id_stored_on_first_creation():
    user_id = uuid.uuid4().hex
    account_id = uuid.uuid4()
    result = await LiberclawService.get_or_create_api_key(
        user_id=user_id, user_type="email", liberclaw_account_id=account_id
    )
    async with AsyncSessionLocal() as db:
        lc_id = (
            await db.execute(
                select(LiberclawUser.id).where(LiberclawUser.user_id == user_id, LiberclawUser.user_type == "email")
            )
        ).scalar_one()
    try:
        assert result.is_new is True
        assert await _stored_account_id(lc_id) == account_id
    finally:
        await _cleanup(lc_id)


async def test_omitting_account_id_leaves_it_null():
    user_id = uuid.uuid4().hex
    await LiberclawService.get_or_create_api_key(user_id=user_id, user_type="email")
    async with AsyncSessionLocal() as db:
        lc_id = (
            await db.execute(
                select(LiberclawUser.id).where(LiberclawUser.user_id == user_id, LiberclawUser.user_type == "email")
            )
        ).scalar_one()
    try:
        assert await _stored_account_id(lc_id) is None
    finally:
        await _cleanup(lc_id)


async def test_account_id_backfilled_on_existing_row_with_none():
    user_id = uuid.uuid4().hex
    await LiberclawService.get_or_create_api_key(user_id=user_id, user_type="email")
    async with AsyncSessionLocal() as db:
        lc_id = (
            await db.execute(
                select(LiberclawUser.id).where(LiberclawUser.user_id == user_id, LiberclawUser.user_type == "email")
            )
        ).scalar_one()
    try:
        assert await _stored_account_id(lc_id) is None

        account_id = uuid.uuid4()
        await LiberclawService.get_or_create_api_key(
            user_id=user_id, user_type="email", liberclaw_account_id=account_id
        )
        assert await _stored_account_id(lc_id) == account_id
    finally:
        await _cleanup(lc_id)


async def test_account_id_never_overwritten_once_set():
    user_id = uuid.uuid4().hex
    original = uuid.uuid4()
    await LiberclawService.get_or_create_api_key(user_id=user_id, user_type="email", liberclaw_account_id=original)
    async with AsyncSessionLocal() as db:
        lc_id = (
            await db.execute(
                select(LiberclawUser.id).where(LiberclawUser.user_id == user_id, LiberclawUser.user_type == "email")
            )
        ).scalar_one()
    try:
        other = uuid.uuid4()
        await LiberclawService.get_or_create_api_key(user_id=user_id, user_type="email", liberclaw_account_id=other)
        assert await _stored_account_id(lc_id) == original
    finally:
        await _cleanup(lc_id)
