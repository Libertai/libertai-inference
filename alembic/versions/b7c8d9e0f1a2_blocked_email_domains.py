"""blocked_email_domains (signup domain blocklist)

Created empty: the list is operational data and this repo is public.

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f7
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blocked_email_domains",
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("domain"),
        # Matching is exact, so a row that is not already normalised would never match anything.
        sa.CheckConstraint("domain = lower(btrim(domain))", name="blocked_email_domains_domain_normalized"),
    )


def downgrade() -> None:
    op.drop_table("blocked_email_domains")
