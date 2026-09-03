"""invoice series and liberclaw owner

Revision ID: a3bcf77e19a2
Revises: e7f8a9b0c1d2
Create Date: 2026-09-03 00:23:14.464434

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3bcf77e19a2"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default kept permanently (not dropped post-backfill): old replicas mid-rolling-deploy
    # INSERT without a series column and would otherwise violate the NOT NULL below.
    op.add_column("invoices", sa.Column("series", sa.String(), nullable=True, server_default="LTAI"))
    op.execute("UPDATE invoices SET series = 'LTAI'")
    op.alter_column("invoices", "series", existing_type=sa.String(), nullable=False)
    op.add_column("invoices", sa.Column("liberclaw_account_id", sa.UUID(), nullable=True))
    op.add_column("invoices", sa.Column("provider_subscription_id", sa.String(), nullable=True))
    op.add_column("invoices", sa.Column("cycle_id", sa.String(), nullable=True))
    op.alter_column("invoices", "user_id", existing_type=sa.UUID(), nullable=True)
    op.drop_constraint("uq_invoices_year_seq", "invoices", type_="unique")
    op.create_unique_constraint("uq_invoices_series_year_seq", "invoices", ["series", "year", "seq"])
    op.create_check_constraint(
        "check_invoice_has_owner", "invoices", "num_nonnulls(user_id, liberclaw_account_id) >= 1"
    )
    op.create_index(
        "ix_invoices_liberclaw_account_id_issued_at", "invoices", ["liberclaw_account_id", "issued_at"], unique=False
    )
    op.add_column("liberclaw_users", sa.Column("liberclaw_account_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_liberclaw_users_liberclaw_account_id"), "liberclaw_users", ["liberclaw_account_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_liberclaw_users_liberclaw_account_id"), table_name="liberclaw_users")
    op.drop_column("liberclaw_users", "liberclaw_account_id")
    op.drop_index("ix_invoices_liberclaw_account_id_issued_at", table_name="invoices")
    op.drop_constraint("check_invoice_has_owner", "invoices", type_="check")
    op.drop_constraint("uq_invoices_series_year_seq", "invoices", type_="unique")
    op.create_unique_constraint("uq_invoices_year_seq", "invoices", ["year", "seq"])
    op.alter_column("invoices", "user_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("invoices", "cycle_id")
    op.drop_column("invoices", "provider_subscription_id")
    op.drop_column("invoices", "liberclaw_account_id")
    op.drop_column("invoices", "series")
