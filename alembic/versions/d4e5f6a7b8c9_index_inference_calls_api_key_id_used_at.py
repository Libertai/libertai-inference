"""index inference_calls (api_key_id, used_at)

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-06-19

inference_calls had only its PK index. The admin key-list gate and the monthly/
liberclaw usage rollups all run SUM(credits_used) WHERE api_key_id = ? AND
used_at >= ?, which seq-scanned the whole (multi-million row) table each time.
Add a composite index so those become index range scans. Created CONCURRENTLY to
avoid locking writes on the large table.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_inference_calls_api_key_id_used_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX_NAME,
            "inference_calls",
            ["api_key_id", "used_at"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="inference_calls",
            postgresql_concurrently=True,
            if_exists=True,
        )
