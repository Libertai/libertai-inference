"""Weekly window columns on anon_chat_usage (second, longer anonymous rate-limit cap).

Revision ID: a9c3e5f7d1b2
Revises: 2d1af291db71
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a9c3e5f7d1b2"
down_revision = "2d1af291db71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("anon_chat_usage", sa.Column("week_started_at", sa.TIMESTAMP(), nullable=True))
    op.add_column("anon_chat_usage", sa.Column("week_count", sa.Integer(), nullable=True))
    # Seed the weekly window from the current daily one for existing rows.
    op.execute("UPDATE anon_chat_usage SET week_started_at = window_started_at, week_count = count")
    op.alter_column("anon_chat_usage", "week_started_at", nullable=False)
    op.alter_column("anon_chat_usage", "week_count", nullable=False)


def downgrade() -> None:
    op.drop_column("anon_chat_usage", "week_count")
    op.drop_column("anon_chat_usage", "week_started_at")
