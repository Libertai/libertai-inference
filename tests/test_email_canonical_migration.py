"""Migration test for the canonical email unique index (runs real alembic)."""

import os
import uuid

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import make_url

from alembic import command

REVISION = "f2a7c1d9e3b4"
PREV = "a9c47e2b81f0"
INDEX_NAME = "uq_users_email_canonical"


def _scratch_url() -> str:
    base = make_url(os.environ["DATABASE_URL"])
    return base.set(database=f"{base.database}_canonmig").render_as_string(hide_password=False)


def _admin_conninfo(url) -> str:
    return f"host={url.host} port={url.port or 5432} user={url.username} password={url.password} dbname=postgres"


def _libpq_conninfo(url) -> str:
    return f"host={url.host} port={url.port or 5432} user={url.username} password={url.password} dbname={url.database}"


@pytest.fixture
def scratch_db():
    url = make_url(_scratch_url())
    with psycopg.connect(_admin_conninfo(url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{url.database}"')
    prev = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        yield url
    finally:
        os.environ["DATABASE_URL"] = prev
        with psycopg.connect(_admin_conninfo(url), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')


def _add_user(conn, email: str | None) -> None:
    conn.execute("INSERT INTO users (id, email) VALUES (%s, %s)", (uuid.uuid4(), email))


def _index_exists(conn) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (f"public.{INDEX_NAME}",)).fetchone()[0] is not None


def test_migration_creates_the_index_and_enforces_one_account_per_canonical_email(scratch_db):
    command.upgrade(Config("alembic.ini"), PREV)
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        _add_user(conn, "someone@gmail.com")
        _add_user(conn, "some.one@example.com")
        _add_user(conn, "someone@example.com")
        _add_user(conn, None)
        _add_user(conn, None)

    command.upgrade(Config("alembic.ini"), REVISION)

    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        assert _index_exists(conn)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _add_user(conn, "some.one+tag@googlemail.com")


def test_migration_aborts_and_lists_the_addresses_when_a_collision_survives(scratch_db):
    command.upgrade(Config("alembic.ini"), PREV)
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        _add_user(conn, "some.one@gmail.com")
        _add_user(conn, "someone+tag@gmail.com")

    with pytest.raises(RuntimeError) as excinfo:
        command.upgrade(Config("alembic.ini"), REVISION)

    message = str(excinfo.value)
    assert "some.one@gmail.com" in message
    assert "someone+tag@gmail.com" in message

    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        # Nothing applied: both rows survive untouched and the index was not created.
        assert not _index_exists(conn)
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 2
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == PREV
