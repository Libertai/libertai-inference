"""Rename credit_transactions.transaction_hash to external_reference.

The column was never onchain-only: Revolut top-ups store "revolut:<order_id>",
upgrade refunds store "upgrade_remainder:<sub_id>" — it is the external
idempotency/dedup reference, so name it that.

Revision ID: b7d20c5e114a
Revises: 9c41f2ab77d0
Create Date: 2026-06-11
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b7d20c5e114a"
down_revision = "9c41f2ab77d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE credit_transactions RENAME COLUMN transaction_hash TO external_reference")
    # The unique constraint (and its backing index) keeps working across a column
    # rename; rename it too so reflection/names stay consistent.
    op.execute(
        "ALTER TABLE credit_transactions RENAME CONSTRAINT "
        "credit_transactions_transaction_hash_key TO credit_transactions_external_reference_key"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE credit_transactions RENAME CONSTRAINT "
        "credit_transactions_external_reference_key TO credit_transactions_transaction_hash_key"
    )
    op.execute("ALTER TABLE credit_transactions RENAME COLUMN external_reference TO transaction_hash")
