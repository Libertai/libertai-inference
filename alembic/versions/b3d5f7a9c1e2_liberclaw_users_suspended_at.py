"""liberclaw_users.suspended_at, synced from LiberClaw.

Revision ID: b3d5f7a9c1e2
Revises: e8f1a3c5d7b9
Create Date: 2026-11-22
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "b3d5f7a9c1e2"
down_revision = "e8f1a3c5d7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("liberclaw_users", sa.Column("suspended_at", sa.TIMESTAMP(), nullable=True))


def downgrade() -> None:
    op.drop_column("liberclaw_users", "suspended_at")
