from cryptography.fernet import Fernet

from src.config import config
from src.utils.encryption import decrypt, encrypt


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setattr(config, "ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "ENCRYPTION_KEY_PREVIOUS", None)
    assert decrypt(encrypt("super-secret")) == "super-secret"


def test_decrypt_with_rotated_key(monkeypatch):
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()

    monkeypatch.setattr(config, "ENCRYPTION_KEY", old)
    monkeypatch.setattr(config, "ENCRYPTION_KEY_PREVIOUS", None)
    token = encrypt("rotate-me")

    # Rotate: new key becomes current, old becomes previous -> still decryptable.
    monkeypatch.setattr(config, "ENCRYPTION_KEY", new)
    monkeypatch.setattr(config, "ENCRYPTION_KEY_PREVIOUS", old)
    assert decrypt(token) == "rotate-me"
