"""API keys

Revision ID: 2384d187fb7a
Revises: f728108ce831
Create Date: 2025-03-28 14:31:12.018387

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2384d187fb7a'
down_revision: Union[str, None] = 'f728108ce831'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('api_keys',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('address', sa.String(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('monthly_limit', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['address'], ['users.address'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('key'),
    sa.UniqueConstraint('address', 'name', name='unique_api_key_name_per_user')
    )
    op.create_table('api_key_usages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('credits_used', sa.Float(), nullable=False),
    sa.Column('used_at', sa.TIMESTAMP(), nullable=False),
    sa.CheckConstraint('credits_used >= 0', name='check_credits_used_non_negative'),
    sa.ForeignKeyConstraint(['key'], ['api_keys.key'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('api_key_usages')
    op.drop_table('api_keys')
    # ### end Alembic commands ###
