"""plan subscription events renewed

Revision ID: d64e36784e9f
Revises: a9c3e5f7d1b2
Create Date: 2026-07-24 16:29:20.644992

``activated`` means a subscription's first successful charge; each later billing cycle is a
``renewed`` event. Ties on created_at break by id so exactly one row per sub stays activated.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d64e36784e9f"
down_revision: Union[str, None] = "a9c3e5f7d1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE plan_subscription_events AS e
        SET event_type = 'renewed'
        WHERE e.event_type = 'activated'
          AND EXISTS (
            SELECT 1 FROM plan_subscription_events AS prior
            WHERE prior.subscription_id = e.subscription_id
              AND prior.event_type = 'activated'
              AND (prior.created_at < e.created_at
                   OR (prior.created_at = e.created_at AND prior.id < e.id))
          )
        """
    )


def downgrade() -> None:
    # Irreversible: the credits provider logged native ``renewed`` events before this
    # migration, so a blanket rename back would corrupt them.
    pass
