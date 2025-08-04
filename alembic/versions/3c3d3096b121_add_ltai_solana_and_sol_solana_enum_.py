"""add_ltai_solana_and_sol_solana_enum_values

Revision ID: 3c3d3096b121
Revises: 915e4e040ba3
Create Date: 2025-08-04 16:48:38.548407

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c3d3096b121'
down_revision: Union[str, None] = '915e4e040ba3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ltai_solana and sol_solana values to credittransactionprovider enum."""
    # Add new enum values
    op.execute("ALTER TYPE credittransactionprovider ADD VALUE IF NOT EXISTS 'ltai_solana';")
    op.execute("ALTER TYPE credittransactionprovider ADD VALUE IF NOT EXISTS 'sol_solana';")
    
    # Drop the old constraint
    op.drop_constraint('check_provider_choices', 'credit_transactions', type_='check')
    
    # Add the new constraint with the new values
    op.create_check_constraint(
        'check_provider_choices',
        'credit_transactions',
        "provider::text IN ('base', 'thirdweb', 'voucher', 'solana', 'ltai_solana', 'sol_solana')"
    )


def downgrade() -> None:
    """No downgrade possible — PostgreSQL does not allow removing enum values."""
    # Drop the new constraint
    op.drop_constraint('check_provider_choices', 'credit_transactions', type_='check')
    
    # Add back the old constraint without the new values
    op.create_check_constraint(
        'check_provider_choices',
        'credit_transactions',
        "provider::text IN ('base', 'thirdweb', 'voucher', 'solana')"
    )
