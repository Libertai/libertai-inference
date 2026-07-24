"""uuid identity swap pk and fks

Revision ID: 491dd7c0450b
Revises: ce9e82ee761a
Create Date: 2026-06-02 15:14:30.898033

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '491dd7c0450b'
down_revision: str | None = 'ce9e82ee761a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Make users.id the primary key and user_id the canonical FK.

    Legacy address columns are kept (now nullable, address unique) as a rollback
    hatch for one release. Order matters: drop address FKs -> drop address PK ->
    add id PK -> drop the redundant id unique.
    """
    # Drop the legacy address-based child FKs (they reference users.address / the old PK).
    op.drop_constraint("credit_transactions_address_fkey", "credit_transactions", type_="foreignkey")
    op.drop_constraint("api_keys_user_address_fkey", "api_keys", type_="foreignkey")

    # Swap the users primary key from address to id.
    op.drop_constraint("users_pkey", "users", type_="primary")
    op.alter_column("users", "address", existing_type=sa.String(), nullable=True)
    op.create_unique_constraint("uq_users_address", "users", ["address"])
    op.create_primary_key("users_pkey", "users", ["id"])
    # Note: uq_users_id (unique on id, from migration A) is intentionally kept — the child
    # user_id FKs are bound to that index, so it cannot be dropped without dropping/recreating
    # all of them. It is harmless redundancy alongside the new primary key.

    # Tighten the child identity columns now that the backfill is the source of truth.
    op.alter_column("credit_transactions", "user_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("credit_transactions", "address", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    """Reverse the PK/FK swap (restores address as the primary key)."""
    op.alter_column("credit_transactions", "user_id", existing_type=sa.UUID(), nullable=True)

    op.drop_constraint("users_pkey", "users", type_="primary")
    op.drop_constraint("uq_users_address", "users", type_="unique")
    op.alter_column("users", "address", existing_type=sa.String(), nullable=False)
    op.create_primary_key("users_pkey", "users", ["address"])

    op.create_foreign_key(
        "api_keys_user_address_fkey", "api_keys", "users", ["user_address"], ["address"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "credit_transactions_address_fkey",
        "credit_transactions",
        "users",
        ["address"],
        ["address"],
        ondelete="CASCADE",
    )
