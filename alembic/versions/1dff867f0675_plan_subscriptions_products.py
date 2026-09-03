"""plan_subscriptions product + liberclaw owner columns

Revision ID: 1dff867f0675
Revises: ca070a853372
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1dff867f0675"
down_revision: str | None = "ca070a853372"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_subscriptions",
        sa.Column("product", sa.String(), nullable=False, server_default="libertai"),
    )
    op.add_column("plan_subscriptions", sa.Column("liberclaw_account_id", sa.UUID(), nullable=True))
    op.add_column(
        "plan_subscriptions",
        sa.Column("provider_cancelled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("plan_subscriptions", "user_id", existing_type=sa.UUID(), nullable=True)

    op.create_check_constraint(
        "ck_plan_subscriptions_owner",
        "plan_subscriptions",
        "num_nonnulls(user_id, liberclaw_account_id) >= 1",
    )
    op.create_index(
        "ix_plan_subscriptions_lclw_account_status",
        "plan_subscriptions",
        ["liberclaw_account_id", "status"],
    )
    op.create_index(
        "uq_one_active_plan_subscription_lclw",
        "plan_subscriptions",
        ["liberclaw_account_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'active', 'overdue')"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM plan_subscriptions WHERE product = 'liberclaw' LIMIT 1")).first():
        raise RuntimeError("cannot downgrade: plan_subscriptions has liberclaw rows")

    op.drop_index("uq_one_active_plan_subscription_lclw", table_name="plan_subscriptions")
    op.drop_index("ix_plan_subscriptions_lclw_account_status", table_name="plan_subscriptions")
    op.drop_constraint("ck_plan_subscriptions_owner", "plan_subscriptions", type_="check")

    op.alter_column("plan_subscriptions", "user_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("plan_subscriptions", "provider_cancelled")
    op.drop_column("plan_subscriptions", "liberclaw_account_id")
    op.drop_column("plan_subscriptions", "product")
