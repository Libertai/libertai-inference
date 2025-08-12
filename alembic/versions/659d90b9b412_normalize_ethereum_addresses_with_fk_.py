"""normalize_ethereum_addresses_with_fk_handling

Revision ID: 659d90b9b412
Revises: cc25520c6876
Create Date: 2025-08-12 15:00:46.109850

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "659d90b9b412"
down_revision: Union[str, None] = "cc25520c6876"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Normalize all Ethereum addresses (starting with 0x) to lowercase, handling duplicates and foreign keys."""

    # Step 1: Temporarily disable foreign key constraints
    op.execute("SET session_replication_role = replica;")

    try:
        # Step 2: Handle duplicate users with case-insensitive addresses
        # Create a temporary table to track which addresses to merge
        op.execute("""
            CREATE TEMPORARY TABLE duplicate_address_mapping AS
            WITH ranked_users AS (
                SELECT 
                    address,
                    LOWER(address) as lowercase_address,
                    created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY LOWER(address) 
                        ORDER BY created_at ASC
                    ) as rn
                FROM users
                WHERE address LIKE '0x%'
                AND LOWER(address) IN (
                    SELECT LOWER(address)
                    FROM users
                    WHERE address LIKE '0x%'
                    GROUP BY LOWER(address)
                    HAVING COUNT(*) > 1
                )
            )
            SELECT 
                old_user.address as old_address,
                keep_user.address as keep_address,
                keep_user.lowercase_address
            FROM ranked_users old_user
            JOIN ranked_users keep_user ON old_user.lowercase_address = keep_user.lowercase_address
            WHERE old_user.rn > 1 AND keep_user.rn = 1
        """)

        # Step 3: Update all foreign key references to point to the address we're keeping
        # Update credit_transactions
        op.execute("""
            UPDATE credit_transactions 
            SET address = dam.keep_address
            FROM duplicate_address_mapping dam
            WHERE credit_transactions.address = dam.old_address
        """)

        # Update api_keys
        op.execute("""
            UPDATE api_keys 
            SET user_address = dam.keep_address
            FROM duplicate_address_mapping dam
            WHERE api_keys.user_address = dam.old_address
        """)

        # Update subscriptions
        op.execute("""
            UPDATE subscriptions 
            SET user_address = dam.keep_address
            FROM duplicate_address_mapping dam
            WHERE subscriptions.user_address = dam.old_address
        """)

        # Step 4: Delete duplicate user records
        op.execute("""
            DELETE FROM users
            WHERE address IN (SELECT old_address FROM duplicate_address_mapping)
        """)

        # Step 5: Now normalize all addresses to lowercase
        # Update users table first (primary key)
        op.execute("""
            UPDATE users 
            SET address = LOWER(address) 
            WHERE address LIKE '0x%'
        """)

        # Update all foreign key tables
        op.execute("""
            UPDATE credit_transactions 
            SET address = LOWER(address) 
            WHERE address LIKE '0x%'
        """)

        op.execute("""
            UPDATE api_keys 
            SET user_address = LOWER(user_address) 
            WHERE user_address LIKE '0x%'
        """)

        op.execute("""
            UPDATE subscriptions 
            SET user_address = LOWER(user_address) 
            WHERE user_address LIKE '0x%'
        """)

        # Clean up temporary table
        op.execute("DROP TABLE IF EXISTS duplicate_address_mapping")

    finally:
        # Step 6: Re-enable foreign key constraints
        op.execute("SET session_replication_role = DEFAULT;")


def downgrade() -> None:
    """Downgrade schema - no operation needed as this is a data normalization."""
    # Note: We cannot reverse this operation as we don't store the original casing
    # and we've merged duplicate users. This is a one-way data normalization operation.
    pass
