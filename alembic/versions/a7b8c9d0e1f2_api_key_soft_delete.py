"""api_keys soft delete (deleted_at)

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-06-02

Adds a nullable ``deleted_at`` to ``api_keys`` so keys can be soft-deleted —
hidden + disabled — without cascading away their inference_calls usage history.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("deleted_at", sa.TIMESTAMP(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "deleted_at")
