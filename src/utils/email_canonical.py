"""Canonical form of an email address, used to decide which addresses are one account.

A ``+tag`` suffix is a subaddress: every mainstream provider (Gmail, Yandex, Outlook,
Fastmail, Proton, iCloud) delivers ``user+anything@`` to ``user@``, so the tag is stripped
on every domain. Dots are folded for gmail.com/googlemail.com only — Google ignores them,
everyone else treats them as significant.

Only addresses with exactly one ``@`` are folded. ``a@b.com@c.com`` is left whole: splitting
it would canonicalise it onto ``a@b.com`` and let an attacker collide with that mailbox's
account. Malformed input is normalised but never rewritten.

The canonical form drives account lookup and the ``users.email`` uniqueness index; the
stored address stays exactly as the user typed it (after the usual strip/lower).
``canonical_email`` and ``CANONICAL_EMAIL_SQL`` must stay equivalent — the lookup only
uses the index if it repeats the indexed expression.
"""

from sqlalchemy import ColumnElement, String, literal_column

GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
_CANONICAL_GMAIL_DOMAIN = "gmail.com"

# Parenthesised so it is valid both as an index element and as a WHERE operand.
# The first branch is the well-formedness guard described above.
CANONICAL_EMAIL_SQL = (
    "(CASE "
    "WHEN length(lower(btrim({col}))) - length(replace(lower(btrim({col})), '@', '')) <> 1 "
    "THEN lower(btrim({col})) "
    "WHEN split_part(lower(btrim({col})), '@', 2) IN ('gmail.com', 'googlemail.com') "
    "THEN replace(split_part(split_part(lower(btrim({col})), '@', 1), '+', 1), '.', '') || '@gmail.com' "
    "ELSE split_part(split_part(lower(btrim({col})), '@', 1), '+', 1) "
    "|| '@' || split_part(lower(btrim({col})), '@', 2) END)"
)


def canonical_email(email: str) -> str:
    """The address's canonical form: the ``+tag`` goes everywhere, dots only on gmail."""
    normalized = email.strip().lower()
    if normalized.count("@") != 1:
        return normalized
    local, _, domain = normalized.partition("@")
    untagged = local.partition("+")[0]
    if domain not in GMAIL_DOMAINS:
        return f"{untagged}@{domain}"
    return f"{untagged.replace('.', '')}@{_CANONICAL_GMAIL_DOMAIN}"


def canonical_email_expression(column: str = "email") -> ColumnElement[str]:
    """The SQL counterpart of ``canonical_email``, over the named email column."""
    return literal_column(CANONICAL_EMAIL_SQL.format(col=column), String)
