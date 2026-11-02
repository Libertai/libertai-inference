"""Run ``alembic upgrade head`` under a Postgres advisory lock.

App replicas run this at boot (see docker-entrypoint.sh); the lock serializes them so
concurrent starts don't race the same migration. Whoever runs second finds the schema
already at head and no-ops. Blocking (not try-lock): every starter must wait for the
schema to be at head before serving.
"""

import os

import psycopg
from alembic.config import Config
from dotenv import load_dotenv

from alembic import command
from src.utils.pg_locks import MIGRATIONS_LOCK_ID

load_dotenv()


def main() -> None:
    url = os.path.expandvars(os.environ["DATABASE_URL"])
    # autocommit: pg_advisory_lock is session-scoped, so the lock survives the commit.
    # Without it this connection sits "idle in transaction" for the whole upgrade and
    # holds a snapshot, which is exactly what CREATE INDEX CONCURRENTLY waits for —
    # d4e5f6a7b8c9 deadlocks here forever on a fresh database, with the index build
    # waiting on a transaction that only ends when this script does.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATIONS_LOCK_ID,))
        try:
            command.upgrade(Config("alembic.ini"), "head")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATIONS_LOCK_ID,))


if __name__ == "__main__":
    main()
