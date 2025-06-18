"""fix solana provider check constraint

Revision ID: 6548cd6fddde
Revises: a10a2f472824
Create Date: 2025-06-18 01:34:56.799420

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6548cd6fddde'
down_revision: Union[str, None] = 'a10a2f472824'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the old constraint
    op.drop_constraint('check_block_number_required_for_provider_libertai', 'credit_transactions', type_='check')
    
    # Add the new constraint with proper text casting
    op.create_check_constraint(
        'check_block_number_required_for_provider_libertai',
        'credit_transactions',
        "(provider::text = 'thirdweb' OR provider::text = 'voucher') OR (provider::text = 'libertai' AND block_number IS NOT NULL) OR (provider::text = 'solana' AND block_number IS NOT NULL)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop the new constraint
    op.drop_constraint('check_block_number_required_for_provider_libertai', 'credit_transactions', type_='check')
    
    # Add back the old constraint
    op.create_check_constraint(
        'check_block_number_required_for_provider_libertai',
        'credit_transactions',
        "(provider = 'thirdweb' OR provider = 'voucher') OR (provider = 'libertai' AND block_number IS NOT NULL) OR (provider = 'solana' AND block_number IS NOT NULL)"
    )
