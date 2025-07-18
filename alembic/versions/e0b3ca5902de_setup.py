"""Setup

Revision ID: e0b3ca5902de
Revises: 
Create Date: 2025-04-22 18:17:11.005165

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e0b3ca5902de'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('users',
    sa.Column('address', sa.String(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
    sa.PrimaryKeyConstraint('address')
    )
    op.create_table('api_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('user_address', sa.String(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('monthly_limit', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['user_address'], ['users.address'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_address', 'name', name='unique_api_key_name_per_user')
    )
    op.create_index(op.f('ix_api_keys_key'), 'api_keys', ['key'], unique=True)
    op.create_table('credit_transactions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('transaction_hash', sa.String(), nullable=True),
    sa.Column('address', sa.String(), nullable=False),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('amount_left', sa.Float(), nullable=False),
    sa.Column('provider', sa.String(), nullable=False),
    sa.Column('block_number', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
    sa.Column('expired_at', sa.TIMESTAMP(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.CheckConstraint("(provider = 'thirdweb' OR provider = 'voucher') OR (provider = 'libertai' AND block_number IS NOT NULL) OR (provider = 'solana' AND block_number IS NOT NULL)", name='check_block_number_required_for_provider_libertai'),
    sa.CheckConstraint("provider IN ('libertai', 'thirdweb', 'voucher', 'solana')", name='check_provider_choices'),
    sa.CheckConstraint('amount >= 0', name='check_amount_non_negative'),
    sa.CheckConstraint('amount_left <= amount', name='check_amount_left_not_exceeding_value'),
    sa.CheckConstraint('amount_left >= 0', name='check_amount_left_non_negative'),
    sa.ForeignKeyConstraint(['address'], ['users.address'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('transaction_hash')
    )
    op.create_table('inference_calls',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('api_key_id', sa.UUID(), nullable=False),
    sa.Column('credits_used', sa.Float(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cached_tokens', sa.Integer(), nullable=False),
    sa.Column('used_at', sa.TIMESTAMP(), nullable=False),
    sa.Column('model_name', sa.String(), nullable=False),
    sa.CheckConstraint('credits_used >= 0', name='check_credits_used_non_negative'),
    sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('inference_calls')
    op.drop_table('credit_transactions')
    op.drop_index(op.f('ix_api_keys_key'), table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_table('users')
    # ### end Alembic commands ###
