"""Per-IP/network signup rate limits.

Revision ID: b2e6c8f0a4d7
Revises: d5e7f9a1b3c5
Create Date: 2026-10-27
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b2e6c8f0a4d7"
down_revision = "d5e7f9a1b3c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signup_rate_limits",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("window_started_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "value", name="uq_signup_rate_limit_scope_value"),
    )


def downgrade() -> None:
    op.drop_table("signup_rate_limits")
