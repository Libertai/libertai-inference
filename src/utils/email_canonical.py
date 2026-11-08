"""Canonical form of an email address, used to decide which addresses are one account.

A ``+tag`` suffix is a subaddress: every mainstream provider (Gmail, Yandex, Outlook,
Fastmail, Proton, iCloud) delivers ``user+anything@`` to ``user@``, so the tag is stripped
on every domain.

Provider-specific differences beyond the tag:
  - Gmail ignores dots; ``googlemail.com`` is the same mailbox as ``gmail.com``.
  - Yandex ignores dots and hyphens, and its country domains (``yandex.ru``, ``yandex.by``,
    ``yandex.kz``, ``ya.ru``) are aliases of ``yandex.com``.
  - iCloud accounts created before 2012 may also receive at ``me.com`` and ``mac.com``.
Dots are significant everywhere else (Proton, Outlook, custom domains), so folding them
would merge mailboxes that belong to different people.

A trailing dot is an FQDN spelling of the same domain, so it is stripped for all domains.

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
ICLOUD_ALIAS_DOMAINS = ("icloud.com", "me.com", "mac.com")
YANDEX_ALIAS_DOMAINS = ("yandex.com", "yandex.ru", "yandex.by", "yandex.kz", "ya.ru")

_CANONICAL_GMAIL_DOMAIN = "gmail.com"
_CANONICAL_ICLOUD_DOMAIN = "icloud.com"
_CANONICAL_YANDEX_DOMAIN = "yandex.com"

# Parenthesised so it is valid both as an index element and as a WHERE operand.
# The first branch is the well-formedness guard described above.
CANONICAL_EMAIL_SQL = (
    "(CASE "
    "WHEN length(lower(btrim({col}))) - length(replace(lower(btrim({col})), '@', '')) <> 1 "
    "THEN lower(btrim({col})) "
    "WHEN rtrim(split_part(lower(btrim({col})), '@', 2), '.') IN ('gmail.com', 'googlemail.com') "
    "THEN replace(split_part(split_part(lower(btrim({col})), '@', 1), '+', 1), '.', '') || '@gmail.com' "
    "WHEN rtrim(split_part(lower(btrim({col})), '@', 2), '.') IN ('icloud.com', 'me.com', 'mac.com') "
    "THEN split_part(split_part(lower(btrim({col})), '@', 1), '+', 1) || '@icloud.com' "
    "WHEN rtrim(split_part(lower(btrim({col})), '@', 2), '.') "
    "IN ('yandex.com', 'yandex.ru', 'yandex.by', 'yandex.kz', 'ya.ru') "
    "THEN replace(replace(split_part(split_part(lower(btrim({col})), '@', 1), '+', 1), '.', ''), '-', '') "
    "|| '@yandex.com' "
    "ELSE split_part(split_part(lower(btrim({col})), '@', 1), '+', 1) "
    "|| '@' || rtrim(split_part(lower(btrim({col})), '@', 2), '.') END)"
)


def canonical_email(email: str) -> str:
    """The address's canonical form: subaddress tags and provider domain aliases folded."""
    normalized = email.strip().lower()
    if normalized.count("@") != 1:
        return normalized
    local, _, domain = normalized.partition("@")
    domain = domain.rstrip(".")
    untagged = local.partition("+")[0]
    if domain in GMAIL_DOMAINS:
        return f"{untagged.replace('.', '')}@{_CANONICAL_GMAIL_DOMAIN}"
    if domain in ICLOUD_ALIAS_DOMAINS:
        return f"{untagged}@{_CANONICAL_ICLOUD_DOMAIN}"
    if domain in YANDEX_ALIAS_DOMAINS:
        return f"{untagged.replace('.', '').replace('-', '')}@{_CANONICAL_YANDEX_DOMAIN}"
    return f"{untagged}@{domain}"


def canonical_email_expression(column: str = "email") -> ColumnElement[str]:
    """The SQL counterpart of ``canonical_email``, over the named email column."""
    return literal_column(CANONICAL_EMAIL_SQL.format(col=column), String)
