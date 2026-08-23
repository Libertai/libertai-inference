"""Rename liberclaw_users tiers to LiberClaw's own plan names (premium -> starter, ultra -> team).

Revision ID: b7d2c9e4a105
Revises: d3e4f5a6b7c8
Create Date: 2026-08-23
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b7d2c9e4a105"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE liberclaw_users SET tier = 'starter' WHERE tier = 'premium'")
    op.execute("UPDATE liberclaw_users SET tier = 'team' WHERE tier = 'ultra'")


def downgrade() -> None:
    op.execute("UPDATE liberclaw_users SET tier = 'premium' WHERE tier = 'starter'")
    op.execute("UPDATE liberclaw_users SET tier = 'ultra' WHERE tier = 'team'")
