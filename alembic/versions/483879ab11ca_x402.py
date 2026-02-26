"""x402

Revision ID: 483879ab11ca
Revises: ade7c91f8970
Create Date: 2026-02-25 17:02:07.533545

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "483879ab11ca"
down_revision: Union[str, None] = "ade7c91f8970"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE apikeytype ADD VALUE IF NOT EXISTS 'x402'")


def downgrade() -> None:
    pass
