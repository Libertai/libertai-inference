"""users.suspended_at + suspension_reason

Revision ID: a1b2c3d4e5f7
Revises: f2a7c1d9e3b4
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "f2a7c1d9e3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("suspended_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("users", sa.Column("suspension_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "suspension_reason")
    op.drop_column("users", "suspended_at")
