"""one account per mailbox: strip the +tag on every domain

Revision ID: c4d8e1f2a3b7
Revises: fce523ba9422
Create Date: 2026-09-08

f2a7c1d9e3b4 folded ``+tag`` for gmail only, so on every other domain one mailbox could
hold unlimited accounts: Yandex, Outlook, Fastmail, Proton and iCloud all deliver
``user+anything@`` to ``user@``. Dots stay gmail-only — Google ignores them, others do not.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8e1f2a3b7"
down_revision: str | None = "9c3f2a71d8e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_users_email_canonical"

# Kept in sync with src.utils.email_canonical (migrations stay standalone).
CANONICAL = "(CASE WHEN length(lower(btrim(email))) - length(replace(lower(btrim(email)), '@', '')) <> 1 THEN lower(btrim(email)) WHEN split_part(lower(btrim(email)), '@', 2) IN ('gmail.com', 'googlemail.com') THEN replace(split_part(split_part(lower(btrim(email)), '@', 1), '+', 1), '.', '') || '@gmail.com' ELSE split_part(split_part(lower(btrim(email)), '@', 1), '+', 1) || '@' || split_part(lower(btrim(email)), '@', 2) END)"

PREVIOUS_CANONICAL = (
    "(CASE WHEN split_part(lower(btrim(email)), '@', 2) IN ('gmail.com', 'googlemail.com') "
    "THEN replace(split_part(split_part(lower(btrim(email)), '@', 1), '+', 1), '.', '') || '@gmail.com' "
    "ELSE lower(btrim(email)) END)"
)

COLLISIONS = """
SELECT canonical, string_agg(email, ', ' ORDER BY email) AS addresses
FROM (SELECT {expr} AS canonical, email FROM users WHERE email IS NOT NULL) s
GROUP BY canonical
HAVING count(*) > 1
ORDER BY canonical
"""


def _guard(expr: str, direction: str) -> None:
    # Same stance as f2a7c1d9e3b4: picking a survivor here would silently destroy that
    # account's credits, keys and subscriptions. Tagged duplicates are resolved by hand.
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
