"""Signup email-domain filter.

Two sources, both consulted at account creation: ``disposable-email-domains``, a static
package list that trails the providers it tracks, and ``blocked_email_domains``, rows
managed operationally. The package floor must be bumped regularly for the first to stay
useful; the second is where domains no public list carries are recorded.
"""

from disposable_email_domains import blocklist  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.blocked_email_domain import BlockedEmailDomain

_BLOCKLIST: frozenset[str] = frozenset(blocklist)


class DisposableEmailError(ValueError):
    """An account was about to be created on a blocked domain."""


def signup_domain(email: str) -> str | None:
    """The address's domain in the form both lists are keyed on.

    Trimmed, lowercased, stripped of the root's trailing dot and folded to punycode, so a
    single domain has a single spelling. ``None`` when there is no ``@``, ``""`` when the
    address carries no domain. Malformed labels keep their plain lowercased form.
    """
    parts = email.strip().lower().rsplit("@", 1)
    if len(parts) != 2:
        return None
    domain = parts[1].rstrip(".")
    try:
        return domain.encode("idna").decode("ascii")
    except UnicodeError:
        return domain


def is_disposable_email(email: str) -> bool:
    """Whether the email's domain is on the static package list."""
    return signup_domain(email) in _BLOCKLIST


async def is_blocked_signup_domain(db: AsyncSession, email: str) -> bool:
    """Whether account creation on this address must be refused, by either source."""
    domain = signup_domain(email)
    if not domain:
        return False
    if is_disposable_email(email):
        return True
    row = (
        (await db.execute(select(BlockedEmailDomain.domain).where(BlockedEmailDomain.domain == domain)))
        .scalars()
        .first()
    )
    return row is not None
