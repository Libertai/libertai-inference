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


def _domain_of(email: str) -> str | None:
    """The address's domain, trimmed and lowercased. None when there is no ``@``."""
    parts = email.strip().lower().rsplit("@", 1)
    return parts[1] if len(parts) == 2 else None


def is_disposable_email(email: str) -> bool:
    """Whether the email's domain is on the static package list."""
    domain = _domain_of(email)
    return domain is not None and domain in _BLOCKLIST


async def is_blocked_signup_domain(db: AsyncSession, email: str) -> bool:
    """Whether account creation on this address must be refused, by either source."""
    domain = _domain_of(email)
    if domain is None:
        return False
    if domain in _BLOCKLIST:
        return True
    row = (
        (await db.execute(select(BlockedEmailDomain.domain).where(BlockedEmailDomain.domain == domain)))
        .scalars()
        .first()
    )
    return row is not None
