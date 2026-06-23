"""Rename the 'power' tier to 'max' on existing subscription rows.

The tier table key changed (power -> max); rows created before the rename would
otherwise fail get_tier() in the entitlement service.

Revision ID: 9c41f2ab77d0
Revises: e1f2a3b4c5d6
Create Date: 2026-06-11
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "9c41f2ab77d0"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE plan_subscriptions SET tier = 'max' WHERE tier = 'power'")
    op.execute("UPDATE plan_subscriptions SET pending_tier = 'max' WHERE pending_tier = 'power'")


def downgrade() -> None:
    op.execute("UPDATE plan_subscriptions SET tier = 'power' WHERE tier = 'max'")
    op.execute("UPDATE plan_subscriptions SET pending_tier = 'power' WHERE pending_tier = 'max'")
