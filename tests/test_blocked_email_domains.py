import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from src.models.blocked_email_domain import BlockedEmailDomain
from src.services.disposable_email import _BLOCKLIST, DisposableEmailError, is_blocked_signup_domain
from src.services.users import get_or_create_user_by_email, get_user_by_email

# Punycode of "blöcked-fixture.test".
IDN_FIXTURE_PUNYCODE = "xn--blcked-fixture-wpb.test"
IDN_FIXTURE_UNICODE = "blöcked-fixture.test"


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


async def test_trailing_dot_does_not_bypass_the_match(db):
    db.add(BlockedEmailDomain(domain="blocked-fixture.test"))
    await db.flush()

    assert await is_blocked_signup_domain(db, "someone@blocked-fixture.test.") is True


async def test_trailing_dot_does_not_bypass_the_package_list(db):
    package_domain = next(iter(_BLOCKLIST))

    assert await is_blocked_signup_domain(db, f"someone@{package_domain}.") is True


async def test_unicode_spelling_matches_the_punycode_row(db):
    db.add(BlockedEmailDomain(domain=IDN_FIXTURE_PUNYCODE))
    await db.flush()

    assert await is_blocked_signup_domain(db, f"someone@{IDN_FIXTURE_UNICODE}") is True
    assert await is_blocked_signup_domain(db, f"someone@{IDN_FIXTURE_PUNYCODE}") is True


async def test_address_without_at_sign_is_not_blocked(db):
    assert await is_blocked_signup_domain(db, "notanemail") is False


async def test_address_without_a_domain_is_not_blocked(db):
    assert await is_blocked_signup_domain(db, "someone@") is False


async def test_signup_without_a_domain_creates_no_user(db):
    with pytest.raises(DisposableEmailError):
        await get_or_create_user_by_email(db, "someone@")

    assert await get_user_by_email(db, "someone@") is None


async def test_row_that_is_not_normalized_is_rejected(db):
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(BlockedEmailDomain(domain="Blocked-Fixture.TEST"))


async def test_a_failing_lookup_refuses_the_signup(db, monkeypatch):
    """The gate fails closed: a database error must reach the caller, never read as "not blocked"."""
    original_execute = db.execute

    async def execute(statement, *args, **kwargs):
        if "blocked_email_domains" in str(statement):
            raise OperationalError("SELECT", {}, Exception("connection lost"))
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", execute)

    with pytest.raises(OperationalError):
        await get_or_create_user_by_email(db, "newcomer@allowed-fixture.test")

    monkeypatch.undo()
    assert await get_user_by_email(db, "newcomer@allowed-fixture.test") is None


async def test_signup_on_listed_domain_creates_no_user(db):
    db.add(BlockedEmailDomain(domain="blocked-fixture.test"))
    await db.flush()

    with pytest.raises(DisposableEmailError):
        await get_or_create_user_by_email(db, "newcomer@blocked-fixture.test")

    assert await get_user_by_email(db, "newcomer@blocked-fixture.test") is None


async def test_signup_on_unlisted_domain_succeeds(db):
    user, created = await get_or_create_user_by_email(db, "newcomer@allowed-fixture.test")

    assert created is True
    assert user.email == "newcomer@allowed-fixture.test"


async def test_account_predating_the_block_still_resolves(db):
    user, created = await get_or_create_user_by_email(db, "early@blocked-fixture.test")
    assert created is True

    db.add(BlockedEmailDomain(domain="blocked-fixture.test"))
    await db.flush()

    same, created_again = await get_or_create_user_by_email(db, "early@blocked-fixture.test")
    assert created_again is False
    assert same.id == user.id
