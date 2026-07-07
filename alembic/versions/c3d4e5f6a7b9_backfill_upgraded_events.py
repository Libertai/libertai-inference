"""backfill upgraded events for historical upgrade pairs

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-07-07

An upgrade is stored as two subscriptions with no link between them: the old sub gets an
``upgrading`` event (metadata ``new_tier``) then ``cancelled_for_upgrade``; the new sub gets its
own ``activated``. Going forward the app logs an ``upgraded`` event (metadata ``from``/``to``) on
the new sub at completion so the pair reads as one activity row. This backfills that event for
past, completed upgrades so history collapses the same way. Idempotent; only touches upgrades
that completed (have ``cancelled_for_upgrade``) and whose new sub lacks an ``upgraded`` event.
"""

import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b9"
down_revision: Union[str, None] = "b2c3d4e5f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Completed upgrades: the parked old sub, its from-tier, target tier, and when the upgrade began.
    completed = conn.execute(
        sa.text(
            """
            SELECT up.subscription_id AS old_sub_id,
                   s_old.user_id      AS user_id,
                   s_old.tier         AS from_tier,
                   up.metadata_json ->> 'new_tier' AS to_tier,
                   up.created_at      AS upgraded_at
            FROM plan_subscription_events up
            JOIN plan_subscriptions s_old ON s_old.id = up.subscription_id
            WHERE up.event_type = 'upgrading'
              AND EXISTS (
                  SELECT 1 FROM plan_subscription_events c
                  WHERE c.subscription_id = up.subscription_id
                    AND c.event_type = 'cancelled_for_upgrade'
              )
            """
        )
    ).fetchall()

    inserted = 0
    for old in completed:
        if not old.to_tier:
            continue
        # The new sub: same user, target tier, activated after the upgrade began, not the old sub,
        # and not already backfilled/recorded. Earliest such activation wins.
        new_sub = conn.execute(
            sa.text(
                """
                SELECT s.id AS new_sub_id,
                       (SELECT a.created_at FROM plan_subscription_events a
                        WHERE a.subscription_id = s.id AND a.event_type = 'activated'
                        ORDER BY a.created_at LIMIT 1) AS activated_at
                FROM plan_subscriptions s
                WHERE s.user_id = :user_id
                  AND s.tier = :to_tier
                  AND s.id <> :old_sub_id
                  AND EXISTS (SELECT 1 FROM plan_subscription_events a
                              WHERE a.subscription_id = s.id AND a.event_type = 'activated'
                                AND a.created_at >= :upgraded_at)
                  AND NOT EXISTS (SELECT 1 FROM plan_subscription_events u
                                  WHERE u.subscription_id = s.id AND u.event_type = 'upgraded')
                ORDER BY activated_at
                LIMIT 1
                """
            ),
            {
                "user_id": old.user_id,
                "to_tier": old.to_tier,
                "old_sub_id": old.old_sub_id,
                "upgraded_at": old.upgraded_at,
            },
        ).fetchone()
        if new_sub is None:
            continue

        conn.execute(
            sa.text(
                """
                INSERT INTO plan_subscription_events
                    (id, subscription_id, event_type, metadata_json, created_at)
                VALUES (:id, :sub_id, 'upgraded', CAST(:meta AS json), :created_at)
                """
            ),
            {
                "id": uuid.uuid4(),
                "sub_id": new_sub.new_sub_id,
                "meta": json.dumps({"from": old.from_tier, "to": old.to_tier, "backfill": True}),
                "created_at": new_sub.activated_at,
            },
        )
        inserted += 1

    print(f"backfilled {inserted} upgraded events")


def downgrade() -> None:
    # Only remove events this migration created (tagged ``backfill``); real upgrade events logged
    # by the app are left intact.
    op.get_bind().execute(
        sa.text(
            "DELETE FROM plan_subscription_events "
            "WHERE event_type = 'upgraded' AND (metadata_json ->> 'backfill') = 'true'"
        )
    )
