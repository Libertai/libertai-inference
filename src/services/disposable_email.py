"""Disposable/temporary email domain filter.

Backed by ``disposable-email-domains``, a static list. It trails the providers it
tracks — mail.tm rotates its public domain and the list follows days later — so the
dependency floor must be bumped regularly for this to stay useful.
"""

from disposable_email_domains import blocklist  # type: ignore[import-untyped]

_BLOCKLIST: frozenset[str] = frozenset(blocklist)


class DisposableEmailError(ValueError):
    """An account was about to be created on a known disposable domain."""


def is_disposable_email(email: str) -> bool:
    """Whether the email's domain is a known disposable/temporary provider."""
    try:
        domain = email.lower().rsplit("@", 1)[1]
    except IndexError:
        return False
    return domain in _BLOCKLIST
