"""inference_calls.tier_credits_used (window vs prepaid split)

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-06-10

Records, per inference call, the portion covered by the tier's entitlement
windows; the remainder was paid from prepaid balance. Window usage now sums
this column so prepaid-paid usage no longer drains the allowance.

Existing rows default to 0: subscriptions were not live before this deploy,
so all historical usage was prepaid-billed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inference_calls",
        sa.Column("tier_credits_used", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "check_tier_credits_used_non_negative",
        "inference_calls",
        "tier_credits_used >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("check_tier_credits_used_non_negative", "inference_calls", type_="check")
    op.drop_column("inference_calls", "tier_credits_used")
