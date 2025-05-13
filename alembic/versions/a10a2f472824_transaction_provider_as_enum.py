"""Transaction provider as enum

Revision ID: a10a2f472824
Revises: b2ddc7842398
Create Date: 2025-04-29 22:50:31.106127

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a10a2f472824"
down_revision: Union[str, None] = "b2ddc7842398"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Step 1: Create the enum type
    provider_enum = sa.Enum("libertai", "thirdweb", "voucher", name="transactionprovider")
    provider_enum.create(op.get_bind())

    # Step 2: Use raw SQL to alter the column with explicit cast
    op.execute("""
        ALTER TABLE credit_transactions
        ALTER COLUMN provider TYPE transactionprovider
        USING provider::transactionprovider
    """)


def downgrade() -> None:
    """Downgrade schema."""
    # Step 1: Revert to VARCHAR using cast
    op.execute("""
        ALTER TABLE credit_transactions
        ALTER COLUMN provider TYPE VARCHAR
        USING provider::text
    """)

    # Step 2: Drop the enum type
    provider_enum = sa.Enum("libertai", "thirdweb", "voucher", name="transactionprovider")
    provider_enum.drop(op.get_bind())
