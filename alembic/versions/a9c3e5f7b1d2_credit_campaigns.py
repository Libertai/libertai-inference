"""credit campaigns: event claim codes, their windows, and claims

Revision ID: a9c3e5f7b1d2
Revises: c4d8e1f2a3b7
Create Date: 2026-10-02 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c3e5f7b1d2"
down_revision: str | None = "c4d8e1f2a3b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALEPH_STATUS = sa.Enum("none", "pending", "sent", "failed", name="campaignalephstatus")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "credit_campaigns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("libertai_amount", sa.Float(), nullable=False),
        sa.Column("aleph_amount", sa.Float(), nullable=False),
        sa.Column("credit_validity_days", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("libertai_amount > 0", name="check_credit_campaign_libertai_amount_positive"),
        sa.CheckConstraint("aleph_amount >= 0", name="check_credit_campaign_aleph_amount_non_negative"),
        sa.CheckConstraint("credit_validity_days > 0", name="check_credit_campaign_validity_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "credit_campaign_windows",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("event_name", sa.String(), nullable=False),
        sa.Column("starts_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("ends_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("max_claims", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name="check_credit_campaign_window_ordered"),
        sa.CheckConstraint("max_claims > 0", name="check_credit_campaign_window_max_claims_positive"),
        sa.ForeignKeyConstraint(["campaign_id"], ["credit_campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_credit_campaign_windows_campaign_id", "credit_campaign_windows", ["campaign_id"])
    op.create_table(
        "credit_campaign_claims",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("window_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email_canonical", sa.String(), nullable=False),
        sa.Column("credit_transaction_id", sa.UUID(), nullable=True),
        sa.Column("libertai_amount", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("aleph_amount", sa.Float(), nullable=False),
        sa.Column("aleph_address", sa.String(), nullable=True),
        sa.Column("aleph_status", ALEPH_STATUS, nullable=False),
        sa.Column("aleph_message", sa.JSON(), nullable=True),
        sa.Column("aleph_item_hash", sa.String(), nullable=True),
        sa.Column("aleph_attempts", sa.Integer(), nullable=False),
        sa.Column("aleph_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["credit_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["window_id"], ["credit_campaign_windows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["credit_transaction_id"], ["credit_transactions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_credit_campaign_claims_user"),
        sa.UniqueConstraint("campaign_id", "email_canonical", name="uq_credit_campaign_claims_email"),
        sa.UniqueConstraint("campaign_id", "aleph_address", name="uq_credit_campaign_claims_aleph_address"),
    )
    op.create_index("ix_credit_campaign_claims_window_id", "credit_campaign_claims", ["window_id"])
    op.create_index("ix_credit_campaign_claims_aleph_status", "credit_campaign_claims", ["aleph_status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_credit_campaign_claims_aleph_status", table_name="credit_campaign_claims")
    op.drop_index("ix_credit_campaign_claims_window_id", table_name="credit_campaign_claims")
    op.drop_table("credit_campaign_claims")
    op.drop_index("ix_credit_campaign_windows_campaign_id", table_name="credit_campaign_windows")
    op.drop_table("credit_campaign_windows")
    op.drop_table("credit_campaigns")
    ALEPH_STATUS.drop(op.get_bind(), checkfirst=True)
