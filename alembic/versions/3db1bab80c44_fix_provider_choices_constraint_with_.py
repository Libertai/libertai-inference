"""fix provider choices constraint with text cast

Revision ID: 3db1bab80c44
Revises: 6548cd6fddde
Create Date: 2025-06-18 01:38:20.477676

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3db1bab80c44'
down_revision: Union[str, None] = '6548cd6fddde'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the old constraint
    op.drop_constraint('check_provider_choices', 'credit_transactions', type_='check')
    
    # Add the new constraint with proper text casting
    op.create_check_constraint(
        'check_provider_choices',
        'credit_transactions',
        "provider::text IN ('libertai', 'thirdweb', 'voucher', 'solana')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop the new constraint
    op.drop_constraint('check_provider_choices', 'credit_transactions', type_='check')
    
    # Add back the old constraint
    op.create_check_constraint(
        'check_provider_choices',
        'credit_transactions',
        "provider IN ('libertai', 'thirdweb', 'voucher', 'solana')"
    )
