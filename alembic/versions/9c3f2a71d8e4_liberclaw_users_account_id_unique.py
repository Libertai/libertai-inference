"""liberclaw_users account_id partial unique

Revision ID: 9c3f2a71d8e4
Revises: 1dff867f0675
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c3f2a71d8e4"
down_revision: str | None = "1dff867f0675"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Dedupe pre-existing duplicates before the unique index can be created: an
    # email change on the LiberClaw side used to mint a second row sharing one
    # account id. Keep the most-recently-created row per account id; NULL the
    # others' account id rather than deleting them — api_keys FK liberclaw_users.id.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY liberclaw_account_id ORDER BY created_at DESC, id DESC
            ) AS rn
            FROM liberclaw_users
            WHERE liberclaw_account_id IS NOT NULL
        )
        UPDATE liberclaw_users
        SET liberclaw_account_id = NULL
        FROM ranked
        WHERE liberclaw_users.id = ranked.id AND ranked.rn > 1
        """
    )
    op.create_index(
        "uq_liberclaw_users_account_id",
        "liberclaw_users",
        ["liberclaw_account_id"],
        unique=True,
        postgresql_where=sa.text("liberclaw_account_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_liberclaw_users_account_id", table_name="liberclaw_users")
