import base64
import secrets
import time
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

CDP_HOST = "api.cdp.coinbase.com"
JWT_TTL_SECONDS = 120


@lru_cache(maxsize=4)
def _load_private_key(secret: str) -> tuple[ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey, str]:
    """Parse a CDP API key secret: PEM EC key (ES256) or base64 Ed25519 seed+pubkey (EdDSA)."""
    # Env vars often carry PEM newlines as literal "\n".
    secret = secret.replace("\\n", "\n")
    try:
        key = serialization.load_pem_private_key(secret.encode(), password=None)
        if isinstance(key, ec.EllipticCurvePrivateKey):
            return key, "ES256"
    except ValueError:
        pass
    decoded = base64.b64decode(secret)
    if len(decoded) != 64:
        raise ValueError("CDP API key secret must be a PEM EC key or a base64 Ed25519 key")
    return ed25519.Ed25519PrivateKey.from_private_bytes(decoded[:32]), "EdDSA"


def cdp_auth_headers(key_id: str, key_secret: str, method: str, path: str) -> dict[str, str]:
    """Bearer JWT for one CDP REST call; the token is bound to `method` + host + `path`."""
    key, algorithm = _load_private_key(key_secret)
    now = int(time.time())
    claims = {
        "sub": key_id,
        "iss": "cdp",
        "nbf": now,
        "exp": now + JWT_TTL_SECONDS,
        "uris": [f"{method} {CDP_HOST}{path}"],
    }
    headers = {"kid": key_id, "typ": "JWT", "nonce": "".join(secrets.choice("0123456789") for _ in range(16))}
    token = jwt.encode(claims, key, algorithm=algorithm, headers=headers)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
