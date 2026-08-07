"""one account per canonical email

Revision ID: f2a7c1d9e3b4
Revises: a9c47e2b81f0
Create Date: 2026-08-07

Gmail treats dots and ``+tag`` suffixes in the local part as noise, so those variants of
one address are one mailbox. The index folds them (gmail.com/googlemail.com only — dots
are significant elsewhere) without touching the stored addresses.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a7c1d9e3b4"
down_revision: str | None = "a9c47e2b81f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_users_email_canonical"

# Kept in sync with src.utils.email_canonical (migrations stay standalone).
CANONICAL = (
    "(CASE WHEN split_part(lower(btrim(email)), '@', 2) IN ('gmail.com', 'googlemail.com') "
    "THEN replace(split_part(split_part(lower(btrim(email)), '@', 1), '+', 1), '.', '') || '@gmail.com' "
    "ELSE lower(btrim(email)) END)"
)

COLLISIONS = f"""
SELECT canonical, string_agg(email, ', ' ORDER BY email) AS addresses
FROM (SELECT {CANONICAL} AS canonical, email FROM users WHERE email IS NOT NULL) s
GROUP BY canonical
HAVING count(*) > 1
ORDER BY canonical
"""


def upgrade() -> None:
    # Accounts that share a canonical address have to be merged by hand: picking a survivor
    # here would silently destroy credits, keys and subscriptions.
    collisions = op.get_bind().execute(sa.text(COLLISIONS)).fetchall()
    if collisions:
        listing = "; ".join(f"{canonical} <- {addresses}" for canonical, addresses in collisions)
        raise RuntimeError(
            f"Cannot create {INDEX_NAME}: {len(collisions)} canonical email collision(s) in users. "
            f"Merge these accounts first, then re-run the migration. {listing}"
        )
    # NULL emails stay unconstrained: NULLs are distinct in a unique index.
    op.execute(f"CREATE UNIQUE INDEX {INDEX_NAME} ON users ({CANONICAL})")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
