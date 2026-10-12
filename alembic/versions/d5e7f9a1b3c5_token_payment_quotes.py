"""token payment quotes: price locks for on-chain top-ups

Revision ID: d5e7f9a1b3c5
Revises: a9c3e5f7b1d2
Create Date: 2026-10-06 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e7f9a1b3c5"
down_revision: str | None = "a9c3e5f7b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "token_payment_quotes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("chain", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("payer_address", sa.String(), nullable=False),
        sa.Column("credit_amount", sa.Float(), nullable=False),
        sa.Column("token_price", sa.Float(), nullable=False),
        sa.Column("token_amount_raw", sa.Numeric(78, 0), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("consumed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("external_reference", sa.String(), nullable=True),
        sa.CheckConstraint("credit_amount > 0", name="check_token_payment_quote_credit_positive"),
        sa.CheckConstraint("token_amount_raw > 0", name="check_token_payment_quote_amount_positive"),
        sa.CheckConstraint("expires_at > created_at", name="check_token_payment_quote_ordered"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_reference"),
    )
    op.create_index("ix_token_payment_quotes_match", "token_payment_quotes", ["payer_address", "token_amount_raw"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_token_payment_quotes_match", table_name="token_payment_quotes")
    op.drop_table("token_payment_quotes")
