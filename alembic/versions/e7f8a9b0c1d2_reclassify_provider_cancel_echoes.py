"""reclassify provider cancel echoes as bookkeeping

Revision ID: e7f8a9b0c1d2
Revises: 278cfb829fbe
Create Date: 2026-08-31

Converts stored ``cancelled`` events that were echoes of a cancel we issued ourselves (the
provider's webhook for it) into the ``provider_cancel_confirmed`` the app now logs. Keyed on an
earlier terminal event on the same subscription, so a genuine provider-side cancel is untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "278cfb829fbe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ECHO_PREDICATE = """
    event_type = :from_type
      AND EXISTS (
          SELECT 1 FROM plan_subscription_events prior
          WHERE prior.subscription_id = plan_subscription_events.subscription_id
            AND prior.created_at <= plan_subscription_events.created_at
            AND prior.id <> plan_subscription_events.id
            AND prior.event_type IN ('cancelled_for_upgrade', 'expired', 'finished',
                                     'expired_abandoned_checkout', 'expired_insufficient_credits')
      )
"""


def _reclassify(from_type: str, to_type: str) -> None:
    result = op.get_bind().execute(
        sa.text(f"UPDATE plan_subscription_events SET event_type = :to_type WHERE {_ECHO_PREDICATE}"),
        {"from_type": from_type, "to_type": to_type},
    )
    print(f"reclassified {result.rowcount} {from_type} -> {to_type}")


def upgrade() -> None:
    _reclassify("cancelled", "provider_cancel_confirmed")


def downgrade() -> None:
    _reclassify("provider_cancel_confirmed", "cancelled")
