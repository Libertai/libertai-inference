"""Anonymous per-IP chat usage counter (rate limit for logged-out chat).

Revision ID: c4f1a9d2e7b8
Revises: b7d20c5e114a
Create Date: 2026-06-16
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c4f1a9d2e7b8"
down_revision = "b7d20c5e114a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anon_chat_usage",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("ip", sa.String(), nullable=False),
        sa.Column("window_started_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ip", name="uq_anon_chat_usage_ip"),
    )


def downgrade() -> None:
    op.drop_table("anon_chat_usage")
