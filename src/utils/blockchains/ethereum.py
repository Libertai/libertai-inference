from eth_account.messages import encode_defunct
from hexbytes import HexBytes
from web3 import Web3


def is_eth_signature_valid(message: str, signature: str, address: str) -> bool:
    """Check if a message signature with an Ethereum wallet is valid"""
    w3 = Web3(Web3.HTTPProvider(""))
    encoded_message = encode_defunct(text=message)
    recovered_address = w3.eth.account.recover_message(
        encoded_message,
        signature=HexBytes(signature),
    )
    return format_eth_address(address) == format_eth_address(recovered_address)


def format_eth_address(address: str) -> str:
    return address.lower()
