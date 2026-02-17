"""merge branches

Revision ID: fce523ba9422
Revises: 3db1bab80c44, 94ae10b846bb
Create Date: 2025-07-18 22:20:54.113125

"""

from typing import Sequence, Union, Tuple

# revision identifiers, used by Alembic.
revision: str = "fce523ba9422"
down_revision: Tuple[str, str] = ("3db1bab80c44", "94ae10b846bb")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
