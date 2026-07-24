"""uuid identity add and backfill

Revision ID: ce9e82ee761a
Revises: 2bccd793c8f4
Create Date: 2026-06-02 15:01:14.478776

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ce9e82ee761a'
down_revision: str | None = '2bccd793c8f4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the UUID identity columns + tables and backfill from the legacy address.

    Additive only: keeps users.address as PK and the address-based child FKs intact.
    The PK/FK swap happens in a later migration. gen_random_uuid() is built-in on PG13+.
    """
    # 1. users.id (UUID) + profile columns. id stays nullable->backfill->not null + unique.
    op.add_column("users", sa.Column("id", sa.UUID(), nullable=True))
    op.execute("UPDATE users SET id = gen_random_uuid() WHERE id IS NULL")
    op.alter_column("users", "id", nullable=False)
    op.create_unique_constraint("uq_users_id", "users", ["id"])
    op.add_column("users", sa.Column("email", sa.String(), nullable=True))
    op.add_column(
        "users", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )
    op.add_column("users", sa.Column("display_name", sa.String(), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(), nullable=True))
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    # The Boolean server_default was only needed to backfill existing rows; drop it to match the model.
    op.alter_column("users", "email_verified", server_default=None)

    # 2. New identity / auth tables (mirror the SQLAlchemy models). FKs target users.id (now unique).
    op.create_table(
        "wallet_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("chain", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chain", "address", name="uq_wallet_connection_chain_address"),
    )
    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.Column("provider_email", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_id", name="uq_oauth_connection_provider_id"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("refresh_token_hash", sa.String(), nullable=False),
        sa.Column("device_info", sa.String(length=500), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_refresh_token_hash"), "sessions", ["refresh_token_hash"], unique=False)
    op.create_table(
        "magic_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_magic_links_email"), "magic_links", ["email"], unique=False)
    op.create_table(
        "auth_codes",
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("access_token", sa.String(), nullable=False),
        sa.Column("refresh_token", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_table(
        "wallet_challenges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("nonce", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_wallet_challenges_address"), "wallet_challenges", ["address"], unique=False)

    # 3. Backfill a wallet_connection per existing user address (chain inferred from the prefix).
    op.execute(
        """
        INSERT INTO wallet_connections (id, user_id, chain, address, is_primary, created_at)
        SELECT gen_random_uuid(), u.id,
               CASE WHEN u.address LIKE '0x%' THEN 'base' ELSE 'solana' END,
               u.address, true, now()
        FROM users u
        WHERE u.address IS NOT NULL
        """
    )

    # 4. Child user_id columns (nullable for now) + backfill by joining the legacy address + FKs.
    op.add_column("credit_transactions", sa.Column("user_id", sa.UUID(), nullable=True))
    op.execute("UPDATE credit_transactions c SET user_id = u.id FROM users u WHERE c.address = u.address")
    op.create_foreign_key(
        "fk_credit_transactions_user_id", "credit_transactions", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )

    op.add_column("api_keys", sa.Column("user_id", sa.UUID(), nullable=True))
    op.execute("UPDATE api_keys a SET user_id = u.id FROM users u WHERE a.user_address = u.address")
    op.create_foreign_key(
        "fk_api_keys_user_id", "api_keys", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )


def downgrade() -> None:
    """Reverse the additive identity migration."""
    op.drop_constraint("fk_api_keys_user_id", "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "user_id")
    op.drop_constraint("fk_credit_transactions_user_id", "credit_transactions", type_="foreignkey")
    op.drop_column("credit_transactions", "user_id")

    op.drop_index(op.f("ix_wallet_challenges_address"), table_name="wallet_challenges")
    op.drop_table("wallet_challenges")
    op.drop_table("auth_codes")
    op.drop_index(op.f("ix_magic_links_email"), table_name="magic_links")
    op.drop_table("magic_links")
    op.drop_index(op.f("ix_sessions_refresh_token_hash"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("oauth_connections")
    op.drop_table("wallet_connections")

    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.drop_constraint("uq_users_id", "users", type_="unique")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "display_name")
    op.drop_column("users", "email_verified")
    op.drop_column("users", "email")
    op.drop_column("users", "id")
