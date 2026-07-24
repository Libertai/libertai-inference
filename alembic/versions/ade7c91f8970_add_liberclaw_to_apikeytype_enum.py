"""add liberclaw to apikeytype enum

Revision ID: ade7c91f8970
Revises: 8524a5dd70f3
Create Date: 2026-02-16 17:21:38.195690

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ade7c91f8970"
down_revision: str | None = "8524a5dd70f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE apikeytype ADD VALUE IF NOT EXISTS 'liberclaw'")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL doesn't support removing values from enums
