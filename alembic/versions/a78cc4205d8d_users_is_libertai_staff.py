"""users.is_libertai_staff (backoffice access flag)

Revision ID: a78cc4205d8d
Revises: c4f1a9d2e7b8
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a78cc4205d8d"
down_revision: str | None = "c4f1a9d2e7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_libertai_staff", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "is_libertai_staff")
