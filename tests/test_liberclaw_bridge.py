"""Liberclaw identity bridge hardening: liberclaw_account_id is unique (partial,
non-null) and resolution prefers it over (user_id, user_type) so an email change
on the LiberClaw side updates the existing row instead of minting a duplicate.

Service-level tests exercise LiberclawService against the committed DB (it opens
its own sessions), so each cleans up its own rows. The partial-unique test uses
the ``db`` fixture directly since it only needs the schema constraint.
"""

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.liberclaw_user import LiberclawUser
from src.services import liberclaw as liberclaw_module
from src.services.liberclaw import LiberclawService

pytestmark = pytest.mark.asyncio


async def _cleanup(lc_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawCreditGrant).where(LiberclawCreditGrant.liberclaw_user_id == lc_id))
        await db.execute(delete(LiberclawUser).where(LiberclawUser.id == lc_id))
        await db.commit()


async def _lc_id_by_account(account_id):
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(select(LiberclawUser.id).where(LiberclawUser.liberclaw_account_id == account_id))
        ).scalar_one()


# --------------------------------------------------------------------- schema


async def test_account_id_partial_unique(db):
    account_id = uuid.uuid4()
    db.add(LiberclawUser(user_id="a@x.test", user_type="email", liberclaw_account_id=account_id))
    await db.flush()
    db.add(LiberclawUser(user_id="b@x.test", user_type="email", liberclaw_account_id=account_id))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_null_account_id_not_constrained(db):
    db.add(LiberclawUser(user_id="c@x.test", user_type="email"))
    db.add(LiberclawUser(user_id="d@x.test", user_type="email"))
    await db.flush()  # both NULL — partial index excludes them, must not conflict


# --------------------------------------------------------------------- resolve_by_account_id


async def test_resolve_by_account_id_returns_none_for_unknown():
    async with AsyncSessionLocal() as db:
        assert await LiberclawService.resolve_by_account_id(db, uuid.uuid4()) is None


# --------------------------------------------------------------------- get_or_create_api_key


async def test_lookup_prefers_account_id_over_email():
    account_id = uuid.uuid4()
    old_email = f"old-{uuid.uuid4().hex}@x.test"
    result = await LiberclawService.get_or_create_api_key(
        user_id=old_email, user_type="email", liberclaw_account_id=account_id
    )
    lc_id = await _lc_id_by_account(account_id)
    try:
        new_email = f"new-{uuid.uuid4().hex}@x.test"
        second = await LiberclawService.get_or_create_api_key(
            user_id=new_email, user_type="email", liberclaw_account_id=account_id
        )
        assert second.key == result.key  # same row's key, not a new one
        assert second.is_new is False

        async with AsyncSessionLocal() as db:
            rows = (
                (await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id)))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].id == lc_id
        assert rows[0].user_id == new_email

        async with AsyncSessionLocal() as db:
            stale = (
                (
                    await db.execute(
                        select(LiberclawUser).where(
                            LiberclawUser.user_id == old_email, LiberclawUser.user_type == "email"
                        )
                    )
                )
                .scalars()
                .first()
            )
        assert stale is None
    finally:
        await _cleanup(lc_id)


async def test_email_fallback_backfills_account_id():
    user_id = uuid.uuid4().hex
    await LiberclawService.get_or_create_api_key(user_id=user_id, user_type="email")  # legacy: no account id
    async with AsyncSessionLocal() as db:
        lc_id = (
            await db.execute(
                select(LiberclawUser.id).where(LiberclawUser.user_id == user_id, LiberclawUser.user_type == "email")
            )
        ).scalar_one()
    try:
        account_id = uuid.uuid4()
        await LiberclawService.get_or_create_api_key(
            user_id=user_id, user_type="email", liberclaw_account_id=account_id
        )
        async with AsyncSessionLocal() as db:
            row = await db.get(LiberclawUser, lc_id)
        assert row.liberclaw_account_id == account_id
    finally:
        await _cleanup(lc_id)


async def test_non_email_user_type_not_refreshed_on_account_hit():
    account_id = uuid.uuid4()
    original_id = f"discord-{uuid.uuid4().hex}"
    await LiberclawService.get_or_create_api_key(
        user_id=original_id, user_type="discord", liberclaw_account_id=account_id
    )
    lc_id = await _lc_id_by_account(account_id)
    try:
        other_id = f"discord-{uuid.uuid4().hex}"
        await LiberclawService.get_or_create_api_key(
            user_id=other_id, user_type="discord", liberclaw_account_id=account_id
        )
        async with AsyncSessionLocal() as db:
            row = await db.get(LiberclawUser, lc_id)
        assert row.user_id == original_id  # refresh guarded to user_type="email"
    finally:
        await _cleanup(lc_id)


# --------------------------------------------------------------------- update_tier


async def test_update_tier_resolves_by_account_id_and_refreshes_email():
    account_id = uuid.uuid4()
    old_email = f"old-{uuid.uuid4().hex}@x.test"
    await LiberclawService.get_or_create_api_key(user_id=old_email, user_type="email", liberclaw_account_id=account_id)
    lc_id = await _lc_id_by_account(account_id)
    try:
        new_email = f"new-{uuid.uuid4().hex}@x.test"
        await LiberclawService.update_tier(
            user_id=new_email, user_type="email", tier="pro", liberclaw_account_id=account_id
        )
        async with AsyncSessionLocal() as db:
            row = await db.get(LiberclawUser, lc_id)
        assert row.tier == "pro"
        assert row.user_id == new_email
    finally:
        await _cleanup(lc_id)


async def test_email_refresh_skipped_on_collision_with_another_row(monkeypatch):
    """Two rows, refresh would collide on unique_liberclaw_user(user_id, user_type):
    must not raise, must not change either row, and must log an error naming both.

    liberclaw's logger has propagate=False (see src/utils/logger.py), out of caplog's
    root-logger capture -- asserts on the logger call directly instead, like
    test_reconcile_invoices.py does.
    """
    logged: list[str] = []
    monkeypatch.setattr(liberclaw_module.logger, "error", lambda msg: logged.append(msg))
    account_id = uuid.uuid4()
    shared_email = f"shared-{uuid.uuid4().hex}@x.test"
    own_email = f"own-{uuid.uuid4().hex}@x.test"

    # The row already holding the email the refresh would move onto.
    await LiberclawService.get_or_create_api_key(user_id=shared_email, user_type="email")
    async with AsyncSessionLocal() as db:
        other_id = (
            await db.execute(
                select(LiberclawUser.id).where(
                    LiberclawUser.user_id == shared_email, LiberclawUser.user_type == "email"
                )
            )
        ).scalar_one()

    # The row under resolution, whose email would be refreshed to the colliding one.
    await LiberclawService.get_or_create_api_key(user_id=own_email, user_type="email", liberclaw_account_id=account_id)
    lc_id = await _lc_id_by_account(account_id)
    try:
        result = await LiberclawService.get_or_create_api_key(
            user_id=shared_email, user_type="email", liberclaw_account_id=account_id
        )
        assert result.is_new is False

        async with AsyncSessionLocal() as db:
            row = await db.get(LiberclawUser, lc_id)
            other = await db.get(LiberclawUser, other_id)
        assert row.user_id == own_email  # refresh skipped, unchanged
        assert other.user_id == shared_email  # untouched

        assert any(str(lc_id) in msg and str(other_id) in msg for msg in logged)
    finally:
        await _cleanup(lc_id)
        await _cleanup(other_id)


async def test_update_tier_falls_back_to_user_id_when_account_unknown():
    user_id = uuid.uuid4().hex
    await LiberclawService.get_or_create_api_key(user_id=user_id, user_type="email")
    async with AsyncSessionLocal() as db:
        lc_id = (
            await db.execute(
                select(LiberclawUser.id).where(LiberclawUser.user_id == user_id, LiberclawUser.user_type == "email")
            )
        ).scalar_one()
    try:
        given_account_id = uuid.uuid4()
        await LiberclawService.update_tier(
            user_id=user_id, user_type="email", tier="starter", liberclaw_account_id=given_account_id
        )
        async with AsyncSessionLocal() as db:
            row = await db.get(LiberclawUser, lc_id)
        assert row.tier == "starter"
        assert row.liberclaw_account_id == given_account_id  # backfilled via the (user_id, user_type) fallback
    finally:
        await _cleanup(lc_id)


# --------------------------------------------------------------------- grant_extra_credits_by_account_id


async def test_grant_extra_credits_by_account_id():
    account_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        lc = LiberclawUser(user_id=uuid.uuid4().hex, user_type="email", liberclaw_account_id=account_id)
        db.add(lc)
        await db.commit()
        lc_id = lc.id
    try:
        ref = f"test:{uuid.uuid4().hex}"
        async with AsyncSessionLocal() as db:
            amount = await LiberclawService.grant_extra_credits_by_account_id(db, account_id, 12.5, ref)
            await db.commit()  # caller owns the transaction
        assert amount == 12.5

        async with AsyncSessionLocal() as db:
            grants = (
                (await db.execute(select(LiberclawCreditGrant).where(LiberclawCreditGrant.liberclaw_user_id == lc_id)))
                .scalars()
                .all()
            )
        assert len(grants) == 1
        assert grants[0].amount == 12.5

        # idempotent retry, different amount, returns the original
        async with AsyncSessionLocal() as db:
            second = await LiberclawService.grant_extra_credits_by_account_id(db, account_id, 99.0, ref)
            await db.commit()
        assert second == 12.5
    finally:
        await _cleanup(lc_id)


async def test_grant_extra_credits_by_account_id_is_flush_only():
    """The by-account variant must never commit the caller's session (the caller runs it
    mid-webhook-transaction): a rollback after the call must leave no grant row behind."""
    account_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        lc = LiberclawUser(user_id=uuid.uuid4().hex, user_type="email", liberclaw_account_id=account_id)
        db.add(lc)
        await db.commit()
        lc_id = lc.id
    try:
        ref = f"test:{uuid.uuid4().hex}"
        async with AsyncSessionLocal() as db:
            amount = await LiberclawService.grant_extra_credits_by_account_id(db, account_id, 12.5, ref)
            assert amount == 12.5
            await db.rollback()

        async with AsyncSessionLocal() as db:
            grants = (
                (await db.execute(select(LiberclawCreditGrant).where(LiberclawCreditGrant.liberclaw_user_id == lc_id)))
                .scalars()
                .all()
            )
        assert grants == []
    finally:
        await _cleanup(lc_id)


async def test_grant_extra_credits_by_account_id_unknown_account_raises():
    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError):
            await LiberclawService.grant_extra_credits_by_account_id(db, uuid.uuid4(), 5.0, f"test:{uuid.uuid4().hex}")
