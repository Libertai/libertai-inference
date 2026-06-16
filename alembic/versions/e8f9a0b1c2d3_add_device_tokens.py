"""add device tokens

Revision ID: e8f9a0b1c2d3
Revises: b7d20c5e114a
Create Date: 2026-06-15

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, None] = "b7d20c5e114a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    platform_enum = sa.Enum("ios", "android", name="deviceplatform")
    platform_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "device_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("token", sa.String(length=4096), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("platform", platform_enum, nullable=False),
        sa.Column("app_version", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_device_tokens_token"), "device_tokens", ["token"], unique=True)
    op.create_index(op.f("ix_device_tokens_user_id"), "device_tokens", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_device_tokens_user_id"), table_name="device_tokens")
    op.drop_index(op.f("ix_device_tokens_token"), table_name="device_tokens")
    op.drop_table("device_tokens")
    sa.Enum("ios", "android", name="deviceplatform").drop(op.get_bind(), checkfirst=True)
