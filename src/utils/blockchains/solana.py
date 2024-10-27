import base64

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from solders.pubkey import Pubkey


def is_solana_signature_valid(message: str, signature: str, address: str) -> bool:
    """Check if a message signature with a Solana wallet is valid

    Args:
        message: Message that was signed
        signature: base64-encoded message signature
        address: base58-encoded public key
    """
    public_key = Pubkey.from_string(address)
    bytes_message = bytes(message, encoding="utf-8")
    bytes_signature = bytes(signature, "utf-8")

    try:
        retrieved_message = VerifyKey(public_key.__bytes__()).verify(
            smessage=bytes_message, signature=base64.b64decode(bytes_signature)
        )
        return bytes_message == retrieved_message
    except BadSignatureError:
        return False
