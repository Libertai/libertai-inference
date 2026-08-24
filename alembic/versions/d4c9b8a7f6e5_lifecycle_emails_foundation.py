"""Lifecycle emails foundation: users.lifecycle_emails_opt_out + lifecycle_email_sends log

Revision ID: d4c9b8a7f6e5
Revises: b7d2c9e4a105
Create Date: 2026-07-22
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d4c9b8a7f6e5"
down_revision = "b7d2c9e4a105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("lifecycle_emails_opt_out", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "lifecycle_email_sends",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("email_type", sa.String(), nullable=False),
        sa.Column("transactional", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lifecycle_email_sends_user_type", "lifecycle_email_sends", ["user_id", "email_type"])
    op.create_index("ix_lifecycle_email_sends_user_sent_at", "lifecycle_email_sends", ["user_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_lifecycle_email_sends_user_sent_at", table_name="lifecycle_email_sends")
    op.drop_index("ix_lifecycle_email_sends_user_type", table_name="lifecycle_email_sends")
    op.drop_table("lifecycle_email_sends")
    op.drop_column("users", "lifecycle_emails_opt_out")
