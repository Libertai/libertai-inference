import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from src.models.user import User
from src.services.oauth import OAuthUserInfo
from src.services.users import get_or_create_user_by_email, get_or_create_user_by_oauth, get_user_by_email
from src.utils.email_canonical import CANONICAL_EMAIL_SQL, canonical_email, canonical_email_expression


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("ab@gmail.com", "ab@gmail.com"),
        ("a.b@gmail.com", "ab@gmail.com"),
        ("a.b.c@gmail.com", "abc@gmail.com"),
        ("ab+tag@gmail.com", "ab@gmail.com"),
        ("a.b+tag+more@gmail.com", "ab@gmail.com"),
        ("A.B@GoogleMail.com", "ab@gmail.com"),
        ("  a.b@gmail.com  ", "ab@gmail.com"),
        ("a.b@example.com", "a.b@example.com"),
        ("a.b+tag@example.com", "a.b+tag@example.com"),
        ("A.B@Example.com", "a.b@example.com"),
        ("not-an-address", "not-an-address"),
    ],
)
def test_canonical_form(address, expected):
    assert canonical_email(address) == expected


@pytest.mark.parametrize(
    "address",
    [
        "ab@gmail.com",
        "a.b+tag@gmail.com",
        "A.B@GoogleMail.com",
        "  a.b@gmail.com  ",
        "a.b+tag@example.com",
        "not-an-address",
        "@gmail.com",
    ],
)
async def test_sql_and_python_canonical_forms_agree(db, address):
    """The lookup only hits the index while both implementations produce the same string."""
    in_sql = (
        await db.execute(text(f"SELECT {CANONICAL_EMAIL_SQL.format(col=':email')}"), {"email": address})
    ).scalar()
    assert in_sql == canonical_email(address)


async def _user_count(db, email: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(canonical_email_expression("users.email") == canonical_email(email))
        )
    ).scalar()


async def test_gmail_dot_variant_resolves_to_the_same_account(db):
    local = uuid.uuid4().hex
    user, created = await get_or_create_user_by_email(db, f"{local}@gmail.com")
    assert created

    same, created_again = await get_or_create_user_by_email(db, f"{local[:4]}.{local[4:]}@gmail.com")
    assert not created_again
    assert same.id == user.id
    assert same.email == f"{local}@gmail.com"


async def test_gmail_tag_variant_resolves_to_the_same_account(db):
    local = uuid.uuid4().hex
    user, _ = await get_or_create_user_by_email(db, f"{local}@gmail.com")

    same, created = await get_or_create_user_by_email(db, f"{local}+newsletter@gmail.com")
    assert not created
    assert same.id == user.id


async def test_googlemail_folds_to_gmail(db):
    local = uuid.uuid4().hex
    user, _ = await get_or_create_user_by_email(db, f"{local}@gmail.com")

    same, created = await get_or_create_user_by_email(db, f"{local}@googlemail.com")
    assert not created
    assert same.id == user.id


async def test_dots_stay_significant_on_other_domains(db):
    local = uuid.uuid4().hex
    user, _ = await get_or_create_user_by_email(db, f"{local}@example.com")

    other, created = await get_or_create_user_by_email(db, f"{local[:4]}.{local[4:]}@example.com")
    assert created
    assert other.id != user.id


async def test_second_signup_via_a_variant_creates_no_second_row(db):
    local = uuid.uuid4().hex
    await get_or_create_user_by_email(db, f"{local}@gmail.com")
    await get_or_create_user_by_email(db, f"{local[:4]}.{local[4:]}+tag@googlemail.com")
    await db.flush()

    assert await _user_count(db, f"{local}@gmail.com") == 1


async def test_variant_insert_is_rejected_by_the_database(db):
    local = uuid.uuid4().hex
    db.add(User(email=f"{local}@gmail.com"))
    await db.flush()

    db.add(User(email=f"{local[:4]}.{local[4:]}@googlemail.com"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_users_without_an_email_are_unconstrained(db):
    db.add(User(address=f"0x{uuid.uuid4().hex}"))
    db.add(User(address=f"0x{uuid.uuid4().hex}"))
    await db.flush()


async def test_lookup_by_variant_finds_the_account(db):
    local = uuid.uuid4().hex
    user, _ = await get_or_create_user_by_email(db, f"{local}@gmail.com")
    await db.flush()

    found = await get_user_by_email(db, f" {local[:4]}.{local[4:]}+tag@GoogleMail.com ")
    assert found is not None
    assert found.id == user.id


async def test_oauth_links_to_an_existing_account_by_variant(db):
    local = uuid.uuid4().hex
    user, _ = await get_or_create_user_by_email(db, f"{local}@gmail.com")
    await db.flush()

    linked, created = await get_or_create_user_by_oauth(
        db,
        OAuthUserInfo(
            provider="google",
            provider_id=f"canon-{local}",
            email=f"{local[:4]}.{local[4:]}@googlemail.com",
            name=None,
            avatar_url=None,
        ),
    )
    assert not created
    assert linked.id == user.id
    assert linked.email == f"{local}@gmail.com"


async def test_lookup_uses_the_canonical_index(db):
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    query = select(User).where(canonical_email_expression("users.email") == canonical_email("a.b@gmail.com"))
    compiled = query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    plan = "\n".join(row[0] for row in (await db.execute(text(f"EXPLAIN {compiled}"))).all())

    assert "uq_users_email_canonical" in plan
