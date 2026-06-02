"""Symmetric encryption for at-rest secrets (one-time auth-code token storage).

Uses Fernet with key rotation: ENCRYPTION_KEY is the current key; if
ENCRYPTION_KEY_PREVIOUS is set, tokens encrypted with it can still be decrypted.
Generate a key with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet, MultiFernet

from src.config import config


def _cipher() -> MultiFernet:
    if not config.ENCRYPTION_KEY:
        raise RuntimeError("ENCRYPTION_KEY is not configured")
    keys = [config.ENCRYPTION_KEY]
    if config.ENCRYPTION_KEY_PREVIOUS:
        keys.append(config.ENCRYPTION_KEY_PREVIOUS)
    return MultiFernet([Fernet(k) for k in keys])


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _cipher().decrypt(token.encode()).decode()
