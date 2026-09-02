"""liberclaw billing details

Revision ID: 4a155de987da
Revises: a3bcf77e19a2
Create Date: 2026-09-03 00:35:54.825933

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a155de987da"
down_revision: str | None = "a3bcf77e19a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "liberclaw_billing_details",
        sa.Column("liberclaw_account_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("address_line1", sa.String(length=200), nullable=True),
        sa.Column("address_line2", sa.String(length=200), nullable=True),
        sa.Column("postal_code", sa.String(length=32), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column("vat_number", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("liberclaw_account_id"),
    )


def downgrade() -> None:
    op.drop_table("liberclaw_billing_details")
