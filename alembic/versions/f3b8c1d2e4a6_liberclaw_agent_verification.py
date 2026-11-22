"""LiberClaw agent verification orders

Revision ID: f3b8c1d2e4a6
Revises: e8f1a3c5d7b9
Create Date: 2026-11-21

One-off $1 Revolut orders backing free-tier agent verification. Settled by the
existing /payments/webhook/revolut handler; the credit is granted to the
LiberClaw account as an extra-credit grant.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f3b8c1d2e4a6"
down_revision = "e8f1a3c5d7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "liberclaw_agent_verifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("liberclaw_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_order_id", sa.String(), nullable=False),
        sa.Column("checkout_url", sa.String(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payment_method_fingerprint", sa.String(), nullable=True),
        sa.Column("paid_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("credited_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="check_liberclaw_verification_amount_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_method_fingerprint"),
    )
    op.create_index(
        "ix_liberclaw_agent_verifications_liberclaw_account_id",
        "liberclaw_agent_verifications",
        ["liberclaw_account_id"],
    )
    op.create_index(
        "ix_liberclaw_agent_verifications_provider_order_id",
        "liberclaw_agent_verifications",
        ["provider_order_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_liberclaw_agent_verifications_provider_order_id",
        table_name="liberclaw_agent_verifications",
    )
    op.drop_index(
        "ix_liberclaw_agent_verifications_liberclaw_account_id",
        table_name="liberclaw_agent_verifications",
    )
    op.drop_table("liberclaw_agent_verifications")
