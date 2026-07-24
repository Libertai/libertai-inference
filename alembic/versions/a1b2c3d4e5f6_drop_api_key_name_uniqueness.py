"""drop api_keys name uniqueness

Revision ID: a1b2c3d4e5f6
Revises: a78cc4205d8d
Create Date: 2026-07-07

Drops the UNIQUE(user_address, name) constraint on ``api_keys`` so a user can
have multiple keys sharing a name (and freely reuse a soft-deleted key's name).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "a78cc4205d8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("unique_api_key_name_per_user", "api_keys", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        "unique_api_key_name_per_user", "api_keys", ["user_address", "name"]
    )
