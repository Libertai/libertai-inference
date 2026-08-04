"""one live cli api key per (user, device name)

Revision ID: a9c47e2b81f0
Revises: d64e36784e9f
Create Date: 2026-08-04

Duplicate cli rows exist because rotate_or_create_cli_api_key did a SELECT-then-INSERT
with nothing to serialise it. Retire the duplicates, then let the DB enforce the rule.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c47e2b81f0"
down_revision: str | None = "d64e36784e9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_api_keys_cli_user_name"

# NULLS LAST is load-bearing: DESC alone is NULLS FIRST, which ranks a never-used row
# above a used one and would retire the device that actually works.
DEDUP = """
WITH ranked AS (
  SELECT k.id,
         row_number() OVER (
           PARTITION BY k.user_id, k.name
           ORDER BY (SELECT max(ic.used_at) FROM inference_calls ic
                     WHERE ic.api_key_id = k.id) DESC NULLS LAST,
                    k.created_at DESC NULLS LAST
         ) AS rn
  FROM api_keys k
  WHERE k.type = 'cli' AND k.deleted_at IS NULL AND k.user_id IS NOT NULL
)
UPDATE api_keys SET deleted_at = LOCALTIMESTAMP, is_active = false
WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
"""


def upgrade() -> None:
    # Both statements share one transaction, so no login can slip a duplicate in between.
    # That rules out CONCURRENTLY, which cannot run inside a transaction and would leave an
    # INVALID index enforcing nothing if the build failed.
    op.execute(DEDUP)
    op.execute(
        f"CREATE UNIQUE INDEX {INDEX_NAME} ON api_keys (user_id, name) "
        "WHERE type = 'cli' AND deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
