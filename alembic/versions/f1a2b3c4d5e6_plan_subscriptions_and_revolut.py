"""plan_subscriptions tables + revolut credit provider

Revision ID: f1a2b3c4d5e6
Revises: 491dd7c0450b
Create Date: 2026-06-02

Adds the provider-agnostic subscription tables (``plan_subscriptions`` +
``plan_subscription_events``) and registers ``revolut`` as a credit-transaction
provider (fiat top-ups, no on-chain block number).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "491dd7c0450b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. plan_subscriptions ---
    op.create_table(
        "plan_subscriptions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_subscription_id", sa.String(), nullable=True),
        sa.Column("provider_customer_id", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("current_period_start", sa.TIMESTAMP(), nullable=True),
        sa.Column("current_period_end", sa.TIMESTAMP(), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pending_tier", sa.String(), nullable=True),
        sa.Column("is_trial", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.current_timestamp(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.current_timestamp(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # At most one live subscription per user (parked "upgrading" rows excluded).
    op.create_index(
        "uq_one_active_plan_subscription",
        "plan_subscriptions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'active', 'overdue')"),
    )
    op.create_index(
        "ix_plan_subscriptions_provider_subscription_id",
        "plan_subscriptions",
        ["provider_subscription_id"],
    )

    # --- 2. plan_subscription_events ---
    op.create_table(
        "plan_subscription_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("provider_event_id", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.current_timestamp(), nullable=True),
        sa.ForeignKeyConstraint(["subscription_id"], ["plan_subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_event_id", name="uq_plan_subscription_event_provider_event_id"),
    )

    # --- 3. add 'revolut' to the credit provider enum + recreate constraints ---
    op.drop_constraint("check_block_number_required", "credit_transactions", type_="check")
    op.drop_constraint("check_provider_choices", "credit_transactions", type_="check")

    credit_transaction_provider = sa.Enum(
        "ltai_base", "ltai_solana", "thirdweb", "voucher", "sol_solana", "revolut",
        name="credittransactionprovider",
    )
    # Move column to text, drop old enum, recreate with the new member, move back.
    op.execute("ALTER TABLE credit_transactions ALTER COLUMN provider TYPE text USING provider::text;")
    op.execute("DROP TYPE credittransactionprovider;")
    credit_transaction_provider.create(op.get_bind(), checkfirst=True)
    op.execute(
        "ALTER TABLE credit_transactions "
        "ALTER COLUMN provider TYPE credittransactionprovider "
        "USING provider::credittransactionprovider;"
    )

    op.create_check_constraint(
        "check_block_number_required",
        "credit_transactions",
        "(provider::text = 'thirdweb' OR provider::text = 'voucher' OR provider::text = 'revolut') "
        "OR (provider::text = 'ltai_base' AND block_number IS NOT NULL) "
        "OR (provider::text = 'ltai_solana' AND block_number IS NOT NULL) "
        "OR (provider::text = 'sol_solana' AND block_number IS NOT NULL)",
    )
    op.create_check_constraint(
        "check_provider_choices",
        "credit_transactions",
        "provider::text IN ('ltai_base', 'ltai_solana', 'thirdweb', 'voucher', 'sol_solana', 'revolut')",
    )


def downgrade() -> None:
    op.drop_constraint("check_block_number_required", "credit_transactions", type_="check")
    op.drop_constraint("check_provider_choices", "credit_transactions", type_="check")

    # Drop any revolut rows before narrowing the enum (they would violate the cast).
    op.execute("DELETE FROM credit_transactions WHERE provider::text = 'revolut';")

    credit_transaction_provider = sa.Enum(
        "ltai_base", "ltai_solana", "thirdweb", "voucher", "sol_solana",
        name="credittransactionprovider",
    )
    op.execute("ALTER TABLE credit_transactions ALTER COLUMN provider TYPE text USING provider::text;")
    op.execute("DROP TYPE credittransactionprovider;")
    credit_transaction_provider.create(op.get_bind(), checkfirst=True)
    op.execute(
        "ALTER TABLE credit_transactions "
        "ALTER COLUMN provider TYPE credittransactionprovider "
        "USING provider::credittransactionprovider;"
    )

    op.create_check_constraint(
        "check_block_number_required",
        "credit_transactions",
        "(provider::text = 'thirdweb' OR provider::text = 'voucher') "
        "OR (provider::text = 'ltai_base' AND block_number IS NOT NULL) "
        "OR (provider::text = 'ltai_solana' AND block_number IS NOT NULL) "
        "OR (provider::text = 'sol_solana' AND block_number IS NOT NULL)",
    )
    op.create_check_constraint(
        "check_provider_choices",
        "credit_transactions",
        "provider::text IN ('ltai_base', 'ltai_solana', 'thirdweb', 'voucher', 'sol_solana')",
    )

    op.drop_table("plan_subscription_events")
    op.drop_index("ix_plan_subscriptions_provider_subscription_id", table_name="plan_subscriptions")
    op.drop_index("uq_one_active_plan_subscription", table_name="plan_subscriptions")
    op.drop_table("plan_subscriptions")
