"""liberclaw_users.credits_limit_override: grandfather subscribers through the 2026-09 repricing

Revision ID: 5e21a0dfd95e
Revises: c4d8e1f2a3b7
Create Date: 2026-09-28

The repricing lowers every LiberClaw tier's rolling-window cap (free 10 -> 5, starter 100 -> 30,
pro 500 -> 100, team 2000 -> 300). Subscribers live at deploy time keep the cap they bought for as
long as they stay on the same tier: the app clears the override on any tier change, including
the drop to free when the subscription ends.

Backfill, for every liberclaw_users row bridged (on liberclaw_account_id; the row's user_id is
an email) to a live, non-trial, paid liberclaw plan_subscriptions row, i.e.
    product = 'liberclaw' AND status IN ('active', 'overdue', 'upgrading') AND NOT is_trial
    AND tier IN ('starter', 'pro', 'team'):
  - ``overdue`` counts: dunning keeps the entitlement while the provider retries the charge.
  - ``upgrading`` counts: start_checkout/override_tier park the live row there while a new
    checkout is open, and the row is restored to ``active`` if that checkout is abandoned.
  - ``pending`` does not: that checkout was never paid. Manual (admin / migrated legacy) rows
    count, so no one who holds a paid cap today sees it cut.
The override is the OLD cap of the live row's tier, and lc_users.tier is aligned to that tier in
the same write: the next renewal pushes the row's tier through set_tier, and a mismatch left in
place would read as a tier change and wipe the override. Mismatches are counted in the output.
An account with several live rows (``upgrading`` sits outside the one-live-row index) takes the
highest tier among them. Only NULL overrides are written, so a re-run changes nothing.

Run scripts/liberclaw_repricing_precheck.sql against prod before deploying: it lists what this
backfill will write, and the paid rows it will NOT cover (those drop to the new caps at once).
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

# DISTINCT ON keeps one live row per user: the highest tier, then the newest row.
BACKFILL = """
WITH live AS (
    SELECT DISTINCT ON (lu.id) lu.id, lu.tier AS stored_tier, ps.tier AS live_tier
    FROM liberclaw_users lu
    JOIN plan_subscriptions ps ON ps.liberclaw_account_id = lu.liberclaw_account_id
    WHERE lu.credits_limit_override IS NULL
      AND ps.product = 'liberclaw'
      AND ps.status IN ('active', 'overdue', 'upgrading')
      AND NOT ps.is_trial
      AND ps.tier IN ('starter', 'pro', 'team')
    ORDER BY lu.id, CASE ps.tier WHEN 'team' THEN 3 WHEN 'pro' THEN 2 ELSE 1 END DESC, ps.created_at DESC
)
UPDATE liberclaw_users lu
SET tier = live.live_tier,
    credits_limit_override = CASE live.live_tier
        WHEN 'starter' THEN :starter
        WHEN 'pro' THEN :pro
        WHEN 'team' THEN :team
    END
FROM live
WHERE lu.id = live.id
RETURNING live.stored_tier <> live.live_tier AS realigned
"""


def backfill(bind) -> tuple[int, int]:
    """(rows grandfathered, of which had lc_users.tier realigned to the live row's tier)."""
    realigned = [row[0] for row in bind.execute(sa.text(BACKFILL), OLD_CREDITS_LIMITS)]
    return len(realigned), sum(realigned)


def upgrade() -> None:
    op.add_column("liberclaw_users", sa.Column("credits_limit_override", sa.Float(), nullable=True))
    grandfathered, realigned = backfill(op.get_bind())
    print(f"grandfathered {grandfathered} liberclaw subscribers ({realigned} had a tier mismatch, realigned)")


def downgrade() -> None:
    # The tier realignment stays: it corrected lc_users.tier to the live row's, which the
    # pre-repricing code wants too.
    op.drop_column("liberclaw_users", "credits_limit_override")
