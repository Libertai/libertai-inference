"""add_sol_solana_credit_tx_provider

Revision ID: cc25520c6876
Revises: 2e85144aa7a1
Create Date: 2025-08-04 17:18:56.566591

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "cc25520c6876"
down_revision: Union[str, None] = "2e85144aa7a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the old constraint
    op.drop_constraint("check_block_number_required_for_provider_libertai", "credit_transactions", type_="check")
    # Drop the old constraint
    op.drop_constraint("check_provider_choices", "credit_transactions", type_="check")

    # 1. Update existing data
    op.execute("UPDATE credit_transactions SET provider = 'ltai_base' WHERE provider = 'base';")
    op.execute("UPDATE credit_transactions SET provider = 'ltai_solana' WHERE provider = 'solana';")

    # 2. Create new enum types
    credit_transaction_provider = sa.Enum(
        "ltai_base", "ltai_solana", "thirdweb", "voucher", "sol_solana", name="credittransactionprovider"
    )
    credit_transaction_provider.create(op.get_bind(), checkfirst=True)

    # 3. Alter columns to use new enum
    op.execute("""
            ALTER TABLE credit_transactions
            ALTER COLUMN provider TYPE credittransactionprovider
            USING provider::text::credittransactionprovider;
        """)

    # Add the new constraint with proper text casting
    op.create_check_constraint(
        "check_block_number_required",
        "credit_transactions",
        "(provider::text = 'thirdweb' OR provider::text = 'voucher') OR (provider::text = 'ltai_base' AND block_number IS NOT NULL) OR (provider::text = 'ltai_solana' AND block_number IS NOT NULL) OR (provider::text = 'sol_solana' AND block_number IS NOT NULL)",
    )
    # Add the new constraint with proper text casting
    op.create_check_constraint(
        "check_provider_choices",
        "credit_transactions",
        "provider::text IN ('ltai_base', 'ltai_solana', 'thirdweb', 'voucher', 'sol_solana')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop the new constraint
    op.drop_constraint("check_block_number_required", "credit_transactions", type_="check")
    # Drop the new constraint
    op.drop_constraint("check_provider_choices", "credit_transactions", type_="check")

    # 1. Add libertai back (needed for data update)
    op.execute("ALTER TYPE credittransactionprovider ADD VALUE IF NOT EXISTS 'libertai';")

    # 2. Revert 'ltai_base' to 'base' and 'ltai_solana' to 'solana'
    op.execute("UPDATE credit_transactions SET provider = 'base' WHERE provider = 'ltai_base';")
    op.execute("UPDATE credit_transactions SET provider = 'solana' WHERE provider = 'ltai_solana';")

    # 3. Recreate old enum types
    transaction_provider = postgresql.ENUM("base", "thirdweb", "voucher", "solana", name="transactionprovider")
    transaction_provider.create(op.get_bind(), checkfirst=True)

    # 4. Alter columns back to old enums
    op.execute("""
            ALTER TABLE credit_transactions
            ALTER COLUMN provider TYPE transactionprovider
            USING provider::text::transactionprovider;
        """)

    # Add back the old constraint
    op.create_check_constraint(
        "check_block_number_required_for_provider_libertai",
        "credit_transactions",
        "(provider = 'thirdweb' OR provider = 'voucher') OR (provider = 'base' AND block_number IS NOT NULL) OR (provider = 'solana' AND block_number IS NOT NULL)",
    )

    # Add back the old constraint
    op.create_check_constraint(
        "check_provider_choices", "credit_transactions", "provider IN ('base', 'thirdweb', 'voucher', 'solana')"
    )
