"""rename_credit_transaction_providers

Revision ID: 955603fa8315
Revises: 53d9d68061c6
Create Date: 2025-07-19 16:33:59.536404

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "955603fa8315"
down_revision: str | None = "53d9d68061c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade():
    """Add 'base' value to transactionprovider enum to allow safe update."""
    op.execute("ALTER TYPE transactionprovider ADD VALUE IF NOT EXISTS 'base';")


def downgrade():
    """No downgrade possible — PostgreSQL does not allow removing enum values."""
