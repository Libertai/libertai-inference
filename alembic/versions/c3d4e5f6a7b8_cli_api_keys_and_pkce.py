"""cli api keys (type + expiry) and auth_code PKCE challenge

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-04 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # New 'cli' value on the ApiKeyType enum (used by CLI-minted keys).
    op.execute("ALTER TYPE apikeytype ADD VALUE IF NOT EXISTS 'cli'")
    # Optional key expiry (CLI keys must be re-minted via `libertai login`).
    op.add_column("api_keys", sa.Column("expires_at", sa.TIMESTAMP(), nullable=True))
    # PKCE challenge stored with the one-time auth code (CLI loopback flow).
    op.add_column("auth_codes", sa.Column("challenge", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("auth_codes", "challenge")
    op.drop_column("api_keys", "expires_at")
    # PostgreSQL doesn't support removing values from enums; 'cli' is left in place.
