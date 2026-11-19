"""Window debt: overflow left uncharged by a monthly cap, carried into entitlement windows.

Revision ID: e8f1a3c5d7b9
Revises: e8f9a0b1c2d3
Create Date: 2026-11-19
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e8f1a3c5d7b9"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("window_debt_credits", sa.Float(), nullable=False, server_default="0"))
    op.add_column("entitlement_windows", sa.Column("debt_credits", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("entitlement_windows", "debt_credits")
    op.drop_column("users", "window_debt_credits")
