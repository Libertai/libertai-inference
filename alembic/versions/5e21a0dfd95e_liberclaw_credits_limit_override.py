"""liberclaw_users.credits_limit_override: grandfather subscribers through the 2026-09 repricing

Revision ID: 5e21a0dfd95e
Revises: c4d8e1f2a3b7
Create Date: 2026-09-28

The repricing lowers every LiberClaw tier's rolling-window cap (free 10 -> 5, starter 100 -> 30,
pro 500 -> 100, team 2000 -> 300). Subscribers live at deploy time keep the cap they bought for as
long as they stay on the same tier: the app clears the override on any tier change, including
the drop to free when the subscription ends.

Backfill, for every liberclaw_users row that:
  - is on a paid tier (starter / pro / team), and
  - owns a live, non-trial liberclaw plan_subscriptions row, i.e.
      product = 'liberclaw' AND status IN ('active', 'overdue') AND NOT is_trial,
    matched on liberclaw_account_id (the identity bridge; the row's user_id is an email).
``overdue`` counts: dunning keeps the entitlement while the provider retries the charge.
``pending`` does not: that checkout was never paid. Manual (admin / migrated legacy) rows count,
so no one who holds a paid cap today sees it cut. The override is the OLD cap of the row's
current tier. Only NULL overrides are written, so a re-run changes nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e21a0dfd95e"
down_revision: str | None = "c4d8e1f2a3b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Pre-repricing caps, frozen here (migrations stay standalone of src.liberclaw_tiers).
OLD_CREDITS_LIMITS = {"starter": 100.0, "pro": 500.0, "team": 2000.0}

BACKFILL = """
UPDATE liberclaw_users lu
SET credits_limit_override = CASE lu.tier
        WHEN 'starter' THEN :starter
        WHEN 'pro' THEN :pro
        WHEN 'team' THEN :team
    END
WHERE lu.credits_limit_override IS NULL
  AND lu.tier IN ('starter', 'pro', 'team')
  AND lu.liberclaw_account_id IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM plan_subscriptions ps
      WHERE ps.liberclaw_account_id = lu.liberclaw_account_id
        AND ps.product = 'liberclaw'
        AND ps.status IN ('active', 'overdue')
        AND NOT ps.is_trial
  )
"""


def backfill(bind) -> int:
    result = bind.execute(sa.text(BACKFILL), OLD_CREDITS_LIMITS)
    return result.rowcount


def upgrade() -> None:
    op.add_column("liberclaw_users", sa.Column("credits_limit_override", sa.Float(), nullable=True))
    print(f"grandfathered {backfill(op.get_bind())} liberclaw subscribers")


def downgrade() -> None:
    op.drop_column("liberclaw_users", "credits_limit_override")
