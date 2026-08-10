from sqlalchemy import select

from src.models.blocked_email_domain import BlockedEmailDomain
from src.services.disposable_email import _BLOCKLIST, is_blocked_signup_domain


async def test_blocked_domain_row_round_trips(db):
    db.add(BlockedEmailDomain(domain="blocked-fixture.test", reason="unit test"))
    await db.flush()

    found = (
        (await db.execute(select(BlockedEmailDomain).where(BlockedEmailDomain.domain == "blocked-fixture.test")))
        .scalars()
        .first()
    )

    assert found is not None
    assert found.reason == "unit test"
    assert found.created_at is not None


async def test_package_list_domain_is_blocked_without_a_row(db):
    """The DB list composes with the package list; it does not replace it."""
    package_domain = next(iter(_BLOCKLIST))

    assert await is_blocked_signup_domain(db, f"someone@{package_domain}") is True


async def test_listed_domain_is_blocked(db):
    db.add(BlockedEmailDomain(domain="blocked-fixture.test"))
    await db.flush()

    assert await is_blocked_signup_domain(db, "someone@blocked-fixture.test") is True


async def test_unlisted_domain_is_not_blocked(db):
    assert await is_blocked_signup_domain(db, "someone@allowed-fixture.test") is False


async def test_match_ignores_case_and_surrounding_whitespace(db):
    db.add(BlockedEmailDomain(domain="blocked-fixture.test"))
    await db.flush()

    assert await is_blocked_signup_domain(db, "  Someone@BLOCKED-Fixture.TEST  ") is True


async def test_address_without_at_sign_is_not_blocked(db):
    assert await is_blocked_signup_domain(db, "notanemail") is False
