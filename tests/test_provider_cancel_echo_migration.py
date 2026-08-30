"""Migration test for reclassifying provider cancel echoes (runs real alembic)."""

import os
import uuid
from datetime import datetime, timedelta

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import make_url

from alembic import command

REVISION = "e7f8a9b0c1d2"
PREV = "278cfb829fbe"


def _scratch_url() -> str:
    base = make_url(os.environ["DATABASE_URL"])
    return base.set(database=f"{base.database}_echomig").render_as_string(hide_password=False)


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


def _sub(conn, user_id, tier, status):
    sub_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO plan_subscriptions (id, user_id, tier, status, provider) VALUES (%s, %s, %s, %s, 'revolut')",
        (sub_id, user_id, tier, status),
    )
    return sub_id


def _event(conn, sub_id, event_type, at):
    conn.execute(
        "INSERT INTO plan_subscription_events (id, subscription_id, event_type, created_at) VALUES (%s, %s, %s, %s)",
        (uuid.uuid4(), sub_id, event_type, at),
    )


def _types(conn, sub_id) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT event_type FROM plan_subscription_events WHERE subscription_id = %s ORDER BY created_at",
            (sub_id,),
        ).fetchall()
    ]


def test_only_echoes_of_an_already_ended_row_are_reclassified(scratch_db):
    cfg = Config("alembic.ini")
    command.upgrade(cfg, PREV)

    now = datetime.now()
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        user_id = uuid.uuid4()
        conn.execute("INSERT INTO users (id) VALUES (%s)", (user_id,))

        upgraded_away = _sub(conn, user_id, "go", "cancelled")
        _event(conn, upgraded_away, "activated", now - timedelta(hours=1))
        _event(conn, upgraded_away, "cancelled_for_upgrade", now)
        _event(conn, upgraded_away, "cancelled", now + timedelta(milliseconds=250))

        churned = _sub(conn, user_id, "plus", "cancelled")
        _event(conn, churned, "activated", now - timedelta(hours=1))
        _event(conn, churned, "cancelled", now)

    command.upgrade(cfg, REVISION)
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        assert _types(conn, upgraded_away) == ["activated", "cancelled_for_upgrade", "provider_cancel_confirmed"]
        assert _types(conn, churned) == ["activated", "cancelled"]

    command.downgrade(cfg, PREV)
    with psycopg.connect(_libpq_conninfo(scratch_db), autocommit=True) as conn:
        assert _types(conn, upgraded_away) == ["activated", "cancelled_for_upgrade", "cancelled"]
        assert _types(conn, churned) == ["activated", "cancelled"]
