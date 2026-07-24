"""merge branches

Revision ID: fce523ba9422
Revises: 3db1bab80c44, 94ae10b846bb
Create Date: 2025-07-18 22:20:54.113125

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "fce523ba9422"
down_revision: tuple[str, str] = ("3db1bab80c44", "94ae10b846bb")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
