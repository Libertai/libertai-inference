"""Canonical form of an email address, used to decide which addresses are one account.

Gmail ignores dots and ``+tag`` suffixes in the local part, so every such variant of a
gmail address is one mailbox. Folding is therefore restricted to gmail.com/googlemail.com:
dots are significant on every other domain.

The canonical form drives account lookup and the ``users.email`` uniqueness index; the
stored address stays exactly as the user typed it (after the usual strip/lower).
``canonical_email`` and ``CANONICAL_EMAIL_SQL`` must stay equivalent — the lookup only
uses the index if it repeats the indexed expression.
"""

from sqlalchemy import ColumnElement, String, literal_column

GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
_CANONICAL_GMAIL_DOMAIN = "gmail.com"

# Parenthesised so it is valid both as an index element and as a WHERE operand.
CANONICAL_EMAIL_SQL = (
    "(CASE WHEN split_part(lower(btrim({col})), '@', 2) IN ('gmail.com', 'googlemail.com') "
    "THEN replace(split_part(split_part(lower(btrim({col})), '@', 1), '+', 1), '.', '') || '@gmail.com' "
    "ELSE lower(btrim({col})) END)"
)


def canonical_email(email: str) -> str:
    """The address's canonical form: gmail dot/tag variants collapse, other domains only normalise."""
    normalized = email.strip().lower()
    local, _, rest = normalized.partition("@")
    domain, _, _ = rest.partition("@")
    if domain not in GMAIL_DOMAINS:
        return normalized
    return f"{local.partition('+')[0].replace('.', '')}@{_CANONICAL_GMAIL_DOMAIN}"


def canonical_email_expression(column: str = "email") -> ColumnElement[str]:
    """The SQL counterpart of ``canonical_email``, over the named email column."""
    return literal_column(CANONICAL_EMAIL_SQL.format(col=column), String)
