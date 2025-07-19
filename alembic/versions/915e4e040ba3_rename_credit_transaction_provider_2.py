"""rename_credit_transaction_provider_2

Revision ID: 915e4e040ba3
Revises: 955603fa8315
Create Date: 2025-07-19 16:42:51.705280

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "915e4e040ba3"
down_revision: Union[str, None] = "955603fa8315"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema and rename enum values."""
    # Drop the old constraint
    op.drop_constraint("check_block_number_required_for_provider_libertai", "credit_transactions", type_="check")
    # Drop the old constraint
    op.drop_constraint("check_provider_choices", "credit_transactions", type_="check")

    # 1. Update existing data
    op.execute("UPDATE credit_transactions SET provider = 'base' WHERE provider = 'libertai';")

    # 2. Create new enum types
    credit_transaction_provider = sa.Enum("base", "thirdweb", "voucher", "solana", name="credittransactionprovider")
    credit_transaction_provider.create(op.get_bind(), checkfirst=True)

    credit_transaction_status = sa.Enum("pending", "completed", "error", name="credittransactionstatus")
    credit_transaction_status.create(op.get_bind(), checkfirst=True)

    # 3. Alter columns to use new enums
    op.execute("""
            ALTER TABLE credit_transactions
            ALTER COLUMN provider TYPE credittransactionprovider
            USING provider::text::credittransactionprovider;
        """)

    op.execute("""
        ALTER TABLE credit_transactions
        ALTER COLUMN status TYPE credittransactionstatus
        USING status::text::credittransactionstatus;
    """)

    # 4. Drop old enum types
    op.execute("DROP TYPE transactionprovider;")
    op.execute("DROP TYPE transactionstatus;")

    # Add the new constraint with proper text casting
    op.create_check_constraint(
        "check_block_number_required_for_provider_libertai",
        "credit_transactions",
        "(provider::text = 'thirdweb' OR provider::text = 'voucher') OR (provider::text = 'base' AND block_number IS NOT NULL) OR (provider::text = 'solana' AND block_number IS NOT NULL)",
    )
    # Add the new constraint with proper text casting
    op.create_check_constraint(
        "check_provider_choices",
        "credit_transactions",
        "provider::text IN ('base', 'thirdweb', 'voucher', 'solana')",
    )


def downgrade() -> None:
    """Revert enum change and data back to original state."""
    # Drop the new constraint
    op.drop_constraint("check_block_number_required_for_provider_libertai", "credit_transactions", type_="check")
    # Drop the new constraint
    op.drop_constraint("check_provider_choices", "credit_transactions", type_="check")

    # 1. Add libertai back (needed for data update)
    op.execute("ALTER TYPE credittransactionprovider ADD VALUE IF NOT EXISTS 'libertai';")

    # 2. Revert 'base' to 'libertai'
    op.execute("UPDATE credit_transactions SET provider = 'libertai' WHERE provider = 'base';")

    # 3. Recreate old enum types
    transaction_provider = postgresql.ENUM("libertai", "thirdweb", "voucher", name="transactionprovider")
    transaction_provider.create(op.get_bind(), checkfirst=True)

    transaction_status = postgresql.ENUM("pending", "completed", name="transactionstatus")
    transaction_status.create(op.get_bind(), checkfirst=True)

    # 4. Alter columns back to old enums
    op.execute("""
            ALTER TABLE credit_transactions
            ALTER COLUMN provider TYPE transactionprovider
            USING provider::text::transactionprovider;
        """)

    op.execute("""
            ALTER TABLE credit_transactions
            ALTER COLUMN status TYPE transactionstatus
            USING status::text::transactionstatus;
        """)

    # 5. Drop new enums
    op.execute("DROP TYPE credittransactionprovider;")
    op.execute("DROP TYPE credittransactionstatus;")

    # Add back the old constraint
    op.create_check_constraint(
        "check_block_number_required_for_provider_libertai",
        "credit_transactions",
        "(provider = 'thirdweb' OR provider = 'voucher') OR (provider = 'base' AND block_number IS NOT NULL) OR (provider = 'solana' AND block_number IS NOT NULL)",
    )

    # Add back the old constraint
    op.create_check_constraint(
        "check_provider_choices", "credit_transactions", "provider IN ('libertai', 'thirdweb', 'voucher', 'solana')"
    )
