"""drop wallet_challenges

Revision ID: d3e4f5a6b7c8
Revises: c9d1e2f3a4b5
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c9d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_wallet_challenges_address"), table_name="wallet_challenges")
    op.drop_table("wallet_challenges")


def downgrade() -> None:
    op.create_table(
        "wallet_challenges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("nonce", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_wallet_challenges_address"), "wallet_challenges", ["address"], unique=False)
