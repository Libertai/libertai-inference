"""Liberclaw extra credits: grants table + per-call consumption column.

Revision ID: 2d1af291db71
Revises: e5f6a7b8c9d0
Create Date: 2026-07-21

Grants hold extra usage credits for Liberclaw users (e.g. the unused remainder
of a plan cycle forfeited by a mid-cycle upgrade), consumed by usage that
overflows the tier's rolling-window cap. ``inference_calls`` gets a nullable
``liberclaw_extra_credits_used`` recording the grant-paid portion of a call
(NULL everywhere except overflowing liberclaw calls), so liberclaw window
usage can sum ``credits_used - coalesce(liberclaw_extra_credits_used, 0)``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2d1af291db71"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "liberclaw_credit_grants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "liberclaw_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("liberclaw_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("amount_left", sa.Float(), nullable=False),
        sa.Column("external_reference", sa.String(), nullable=False, unique=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("amount > 0", name="check_liberclaw_grant_amount_positive"),
        sa.CheckConstraint("amount_left >= 0", name="check_liberclaw_grant_amount_left_non_negative"),
    )
    op.create_index(
        "ix_liberclaw_credit_grants_user_id", "liberclaw_credit_grants", ["liberclaw_user_id"]
    )

    op.add_column(
        "inference_calls",
        sa.Column("liberclaw_extra_credits_used", sa.Float(), nullable=True),
    )
    op.create_check_constraint(
        "check_liberclaw_extra_credits_used_non_negative",
        "inference_calls",
        "liberclaw_extra_credits_used IS NULL OR liberclaw_extra_credits_used >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "check_liberclaw_extra_credits_used_non_negative", "inference_calls", type_="check"
    )
    op.drop_column("inference_calls", "liberclaw_extra_credits_used")
    op.drop_index("ix_liberclaw_credit_grants_user_id", table_name="liberclaw_credit_grants")
    op.drop_table("liberclaw_credit_grants")
