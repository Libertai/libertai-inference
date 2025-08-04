"""remove_solana_provider_from_constraint

Revision ID: ddb846fe0694
Revises: 3c3d3096b121
Create Date: 2025-08-04 16:49:12.294983

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ddb846fe0694'
down_revision: Union[str, None] = '3c3d3096b121'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove solana provider from constraint, keeping only ltai_solana and sol_solana."""
    # Drop the old constraint
    op.drop_constraint('check_provider_choices', 'credit_transactions', type_='check')
    op.drop_constraint('check_block_number_required_for_provider_libertai', 'credit_transactions', type_='check')
    
    # Add the new constraint without 'solana'
    op.create_check_constraint(
        'check_provider_choices',
        'credit_transactions',
        "provider::text IN ('base', 'thirdweb', 'voucher', 'ltai_solana', 'sol_solana')"
    )
    
    # Update the block number constraint
    op.create_check_constraint(
        'check_block_number_required_for_provider_libertai',
        'credit_transactions',
        "(provider::text = 'thirdweb' OR provider::text = 'voucher') OR (provider::text = 'base' AND block_number IS NOT NULL) OR (provider::text = 'ltai_solana' AND block_number IS NOT NULL) OR (provider::text = 'sol_solana' AND block_number IS NOT NULL)"
    )


def downgrade() -> None:
    """Add back solana provider to constraint."""
    # Drop the new constraint
    op.drop_constraint('check_provider_choices', 'credit_transactions', type_='check')
    op.drop_constraint('check_block_number_required_for_provider_libertai', 'credit_transactions', type_='check')
    
    # Add back the old constraint with 'solana'
    op.create_check_constraint(
        'check_provider_choices',
        'credit_transactions',
        "provider::text IN ('base', 'thirdweb', 'voucher', 'solana', 'ltai_solana', 'sol_solana')"
    )
    
    # Add back the old block number constraint
    op.create_check_constraint(
        'check_block_number_required_for_provider_libertai',
        'credit_transactions',
        "(provider::text = 'thirdweb' OR provider::text = 'voucher') OR (provider::text = 'base' AND block_number IS NOT NULL) OR (provider::text = 'solana' AND block_number IS NOT NULL)"
    )
