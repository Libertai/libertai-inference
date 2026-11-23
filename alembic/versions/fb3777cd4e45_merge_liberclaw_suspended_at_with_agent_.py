"""merge liberclaw suspended_at with agent verification

Revision ID: fb3777cd4e45
Revises: b3d5f7a9c1e2, f3b8c1d2e4a6
Create Date: 2026-11-23 07:35:29.743997

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "fb3777cd4e45"
down_revision: tuple[str, str] = ("b3d5f7a9c1e2", "f3b8c1d2e4a6")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
