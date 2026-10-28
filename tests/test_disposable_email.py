import uuid

import pytest

from src.models.user import User
from src.services import disposable_email
from src.services.disposable_email import _BLOCKLIST, DisposableEmailError, is_disposable_email
from src.services.users import get_or_create_user_by_email

DISPOSABLE = next(iter(_BLOCKLIST))


def test_known_disposable_domain_is_flagged():
    assert is_disposable_email(f"x@{DISPOSABLE}")


@pytest.mark.parametrize("email", ["a@gmail.com", "a@proton.me", "a@icloud.com", "a@outlook.com"])
def test_mainstream_providers_are_not_flagged(email):
    assert not is_disposable_email(email)


def test_domain_match_ignores_case_and_local_part_noise():
    assert is_disposable_email(f"Mixed.Case+tag@{DISPOSABLE.upper()}")


def test_address_without_domain_is_not_flagged():
    assert not is_disposable_email("no-at-sign")


@pytest.mark.asyncio
async def test_signup_on_disposable_domain_is_refused(db):
    with pytest.raises(DisposableEmailError):
        await get_or_create_user_by_email(db, f"{uuid.uuid4().hex}@{DISPOSABLE}")


@pytest.mark.asyncio
async def test_existing_account_on_disposable_domain_still_resolves(db):
    """Only creation is gated — the filter must not lock out accounts that predate it."""
    email = f"{uuid.uuid4().hex}@{DISPOSABLE}"
    db.add(User(email=email))
    await db.flush()

    user, created = await get_or_create_user_by_email(db, email)
    assert not created
    assert user.email == email


@pytest.mark.asyncio
async def test_signup_on_regular_domain_still_works(db):
    user, created = await get_or_create_user_by_email(db, f"{uuid.uuid4().hex}@gmail.com")
    assert created
    assert user.id is not None


def test_subdomain_of_a_package_list_domain_is_flagged():
    assert is_disposable_email(f"x@2y.{DISPOSABLE}")


def test_lookalike_package_domain_is_not_flagged():
    assert not is_disposable_email(f"x@{DISPOSABLE}.example")


def test_disposable_mx_host_matches_subdomains():
    assert disposable_email._is_disposable_mx_host("mail.duckmail.sbs")
    assert disposable_email._is_disposable_mx_host("duckmail.sbs")
    assert disposable_email._is_disposable_mx_host("smtp.215.im")
    assert not disposable_email._is_disposable_mx_host("aspmx.l.google.com")
    assert not disposable_email._is_disposable_mx_host("duckmail.sbs.example")


@pytest.mark.asyncio
async def test_custom_domain_on_disposable_mx_is_blocked(monkeypatch):
    async def fake_mx(_domain):
        return ["mail.duckmail.sbs"]

    monkeypatch.setattr(disposable_email, "resolve_mx_hosts", fake_mx)
    assert await disposable_email.is_disposable_mx_email("new@rotating-custom.shop") is True


@pytest.mark.asyncio
async def test_custom_domain_on_normal_mx_is_allowed(monkeypatch):
    async def fake_mx(_domain):
        return ["aspmx.l.google.com"]

    monkeypatch.setattr(disposable_email, "resolve_mx_hosts", fake_mx)
    assert await disposable_email.is_disposable_mx_email("new@legit-business.shop") is False


@pytest.mark.asyncio
async def test_mx_lookup_failure_fails_open(monkeypatch):
    async def fake_mx(_domain):
        return []

    monkeypatch.setattr(disposable_email, "resolve_mx_hosts", fake_mx)
    assert await disposable_email.is_disposable_mx_email("new@legit-business.shop") is False
