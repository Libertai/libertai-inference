"""drop agents and agent subscriptions

Revision ID: 2bccd793c8f4
Revises: 483879ab11ca
Create Date: 2026-06-02 14:42:19.774463

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2bccd793c8f4'
down_revision: str | None = '483879ab11ca'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the agent-hosting feature and its (agent) subscription billing.

    Order matters: subscription_transactions -> agents -> subscriptions (FK deps),
    then the now-orphaned enum types.
    """
    op.drop_table("subscription_transactions")
    op.drop_table("agents")
    op.drop_table("subscriptions")
    op.execute("DROP TYPE IF EXISTS subscriptiontransactionstatus")
    op.execute("DROP TYPE IF EXISTS subscriptionstatus")
    op.execute("DROP TYPE IF EXISTS subscriptiontype")


def downgrade() -> None:
    """Permanent removal — not reversible (matches repo convention for cleanup migrations)."""
