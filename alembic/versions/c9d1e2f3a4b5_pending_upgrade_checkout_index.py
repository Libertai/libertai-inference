"""pending_upgrade checkout index

Revision ID: c9d1e2f3a4b5
Revises: b7c8d9e0f1a2
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d1e2f3a4b5"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Non-concurrent on purpose: scripts/migrate.py holds an advisory lock on a separate
    # connection for the whole upgrade, and CREATE INDEX CONCURRENTLY waits on that
    # transaction's virtual xid forever. plan_subscriptions is small, so the ACCESS
    # EXCLUSIVE lock is held for milliseconds.
    op.create_index(
        "uq_one_upgrade_checkout",
        "plan_subscriptions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending_upgrade'"),
    )


def downgrade() -> None:
    op.drop_index("uq_one_upgrade_checkout", table_name="plan_subscriptions")
