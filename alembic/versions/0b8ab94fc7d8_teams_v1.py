"""Teams v1: teams, memberships, invites, team balance, ledger; seat + team_id columns.

Revision ID: 0b8ab94fc7d8
Revises: c4f1a9d2e7b8
Create Date: 2026-07-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0b8ab94fc7d8"
down_revision: Union[str, None] = "c4f1a9d2e7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("seat_prices", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("extra_credits_monthly_cap", sa.Float(), nullable=True),
        sa.Column("extra_credits_member_default_cap", sa.Float(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_table(
        "team_memberships",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("team_id", sa.UUID(), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("role", sa.String(), nullable=False, server_default="member"),
        sa.Column("extra_credits_cap_override", sa.Float(), nullable=True),
        sa.Column("invited_by", sa.UUID(), nullable=True),
        sa.Column("joined_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_team_memberships_team_id", "team_memberships", ["team_id"])
    op.create_table(
        "team_invites",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("team_id", sa.UUID(), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_table(
        "team_credit_transactions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("team_id", sa.UUID(), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_reference", sa.String(), nullable=True, unique=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("amount_left", sa.Float(), nullable=False),
        sa.Column(
            "provider",
            postgresql.ENUM(name="credittransactionprovider", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="credittransactionstatus", create_type=False),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("amount >= 0", name="check_team_tx_amount_non_negative"),
        sa.CheckConstraint("amount_left >= 0", name="check_team_tx_amount_left_non_negative"),
        sa.CheckConstraint("amount_left <= amount", name="check_team_tx_amount_left_not_exceeding"),
    )
    op.create_index("ix_team_credit_transactions_team_id", "team_credit_transactions", ["team_id"])
    op.create_table(
        "team_ledger_entries",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("team_id", sa.UUID(), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entry_type", sa.String(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("amount >= 0", name="check_ledger_amount_non_negative"),
    )
    op.create_index("ix_team_ledger_entries_team_id", "team_ledger_entries", ["team_id"])

    op.add_column(
        "plan_subscriptions",
        sa.Column("team_id", sa.UUID(), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("plan_subscriptions", sa.Column("seat_price_snapshot", sa.Float(), nullable=True))
    op.add_column(
        "inference_calls",
        sa.Column("team_id", sa.UUID(), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
    )
    # inference_calls is the hottest table — build this index CONCURRENTLY (outside the
    # migration's transaction) so it never takes a write-blocking lock in production.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_inference_calls_team_id_used_at",
            "inference_calls",
            ["team_id", "used_at"],
            postgresql_where=sa.text("team_id IS NOT NULL"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_inference_calls_team_id_used_at",
            table_name="inference_calls",
            postgresql_concurrently=True,
        )
    op.drop_column("inference_calls", "team_id")
    op.drop_column("plan_subscriptions", "seat_price_snapshot")
    op.drop_column("plan_subscriptions", "team_id")
    op.drop_table("team_ledger_entries")
    op.drop_table("team_credit_transactions")
    op.drop_table("team_invites")
    op.drop_table("team_memberships")
    op.drop_table("teams")
