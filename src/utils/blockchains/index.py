from src.interfaces.subscription import SubscriptionChain
from src.utils.blockchains.ethereum import is_eth_signature_valid
from src.utils.blockchains.solana import is_solana_signature_valid


def is_signature_valid(chain: SubscriptionChain, message: str, signature: str, address: str) -> bool:
    valid = False

    if chain == SubscriptionChain.base:
        valid = is_eth_signature_valid(message, signature, address)
    elif chain == SubscriptionChain.solana:
        valid = is_solana_signature_valid(message, signature, address)

    return valid
