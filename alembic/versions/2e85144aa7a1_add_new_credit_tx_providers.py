"""rename_credit_transaction_providers_and_add_sol

Revision ID: 2e85144aa7a1
Revises: 915e4e040ba3
Create Date: 2025-08-04 17:06:57.475691

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2e85144aa7a1"
down_revision: Union[str, None] = "915e4e040ba3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema and rename enum values."""
    op.execute("ALTER TYPE credittransactionprovider ADD VALUE IF NOT EXISTS 'ltai_base';")
    op.execute("ALTER TYPE credittransactionprovider ADD VALUE IF NOT EXISTS 'ltai_solana';")
    op.execute("ALTER TYPE credittransactionprovider ADD VALUE IF NOT EXISTS 'sol_solana';")


def downgrade() -> None:
    """Revert enum change and data back to original state."""
    pass
