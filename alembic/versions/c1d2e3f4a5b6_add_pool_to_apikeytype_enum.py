"""add pool to apikeytype enum

Revision ID: c1d2e3f4a5b6
Revises: c3d4e5f6a7b8
Create Date: 2026-06-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TYPE apikeytype ADD VALUE IF NOT EXISTS 'pool'")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL doesn't support removing values from enums
