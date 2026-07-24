"""users.monthly_extra_credit_cap (per-user monthly cap on extra-credit overflow spend)

NULL means unlimited (the default). Enforced at the gateway whitelist, not at billing time.

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b9
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "c3d4e5f6a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("monthly_extra_credit_cap", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "monthly_extra_credit_cap")
