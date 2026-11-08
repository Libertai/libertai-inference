"""provider domain aliases in the canonical email index

Revision ID: e8f9a0b1c2d3
Revises: b2e6c8f0a4d7
Create Date: 2026-11-08

The ``+tag`` was already folded, but the domain stayed verbatim. A Yandex mailbox also
receives at its country domains (yandex.ru, yandex.by, yandex.kz, ya.ru) and ignores dots
and hyphens in the local part; legacy iCloud accounts share a local part across icloud.com,
me.com and mac.com. A ``+tag`` on a sibling domain therefore minted a new account from the
same inbox. This index folds those provider aliases and a trailing FQDN dot.

Domains where aliasing is not documented stay literal: Proton's proton.me/protonmail.com are
chosen at signup, Outlook's hotmail.com/outlook.com are separate, and custom-domain catch-alls
are not guessed. Those still need the per-network free-tier cost bound.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
down_revision: str | None = "b2e6c8f0a4d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_users_email_canonical"

# Kept in sync with src.utils.email_canonical (migrations stay standalone).
CANONICAL = (
    "(CASE "
    "WHEN length(lower(btrim(email))) - length(replace(lower(btrim(email)), '@', '')) <> 1 "
    "THEN lower(btrim(email)) "
    "WHEN rtrim(split_part(lower(btrim(email)), '@', 2), '.') IN ('gmail.com', 'googlemail.com') "
    "THEN replace(split_part(split_part(lower(btrim(email)), '@', 1), '+', 1), '.', '') || '@gmail.com' "
    "WHEN rtrim(split_part(lower(btrim(email)), '@', 2), '.') IN ('icloud.com', 'me.com', 'mac.com') "
    "THEN split_part(split_part(lower(btrim(email)), '@', 1), '+', 1) || '@icloud.com' "
    "WHEN rtrim(split_part(lower(btrim(email)), '@', 2), '.') "
    "IN ('yandex.com', 'yandex.ru', 'yandex.by', 'yandex.kz', 'ya.ru') "
    "THEN replace(replace(split_part(split_part(lower(btrim(email)), '@', 1), '+', 1), '.', ''), '-', '') "
    "|| '@yandex.com' "
    "ELSE split_part(split_part(lower(btrim(email)), '@', 1), '+', 1) "
    "|| '@' || rtrim(split_part(lower(btrim(email)), '@', 2), '.') END)"
)

PREVIOUS_CANONICAL = (
    "(CASE "
    "WHEN length(lower(btrim(email))) - length(replace(lower(btrim(email)), '@', '')) <> 1 "
    "THEN lower(btrim(email)) "
    "WHEN split_part(lower(btrim(email)), '@', 2) IN ('gmail.com', 'googlemail.com') "
    "THEN replace(split_part(split_part(lower(btrim(email)), '@', 1), '+', 1), '.', '') || '@gmail.com' "
    "ELSE split_part(split_part(lower(btrim(email)), '@', 1), '+', 1) "
    "|| '@' || split_part(lower(btrim(email)), '@', 2) END)"
)

COLLISIONS = """
SELECT canonical, string_agg(email, ', ' ORDER BY email) AS addresses
FROM (SELECT {expr} AS canonical, email FROM users WHERE email IS NOT NULL) s
GROUP BY canonical
HAVING count(*) > 1
ORDER BY canonical
"""


def _guard(expr: str, direction: str) -> None:
    # Same stance as the earlier canonical-email migrations: picking a survivor here would
    # silently destroy that account's credits, keys and subscriptions. Alias duplicates are
    # resolved by hand, then the migration is re-run.
    collisions = op.get_bind().execute(sa.text(COLLISIONS.format(expr=expr))).fetchall()
    if collisions:
        listing = "; ".join(f"{canonical} <- {addresses}" for canonical, addresses in collisions)
        raise RuntimeError(
            f"Cannot {direction} {INDEX_NAME}: {len(collisions)} canonical email collision(s) in users. "
            f"Merge these accounts first, then re-run the migration. {listing}"
        )


def upgrade() -> None:
    _guard(CANONICAL, "create")
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.execute(f"CREATE UNIQUE INDEX {INDEX_NAME} ON users ({CANONICAL})")


def downgrade() -> None:
    # The old expression is strictly weaker, so it cannot collide where the new one did not.
    _guard(PREVIOUS_CANONICAL, "restore")
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.execute(f"CREATE UNIQUE INDEX {INDEX_NAME} ON users ({PREVIOUS_CANONICAL})")
