"""uq invoices lclw sub cycle

Revision ID: ca070a853372
Revises: 4a155de987da
Create Date: 2026-09-03 00:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ca070a853372"
down_revision: str | None = "4a155de987da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_invoices_lclw_sub_cycle",
        "invoices",
        ["provider_subscription_id", "cycle_id"],
        unique=True,
        postgresql_where=sa.text("series = 'LCLW' AND cycle_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_invoices_lclw_sub_cycle", table_name="invoices")
