from solders.pubkey import Pubkey
from web3 import Web3


def validate_and_format_address(address: str) -> str:
    """
    Validates and formats an address for either Ethereum or Solana.
    
    For Ethereum addresses, returns the checksummed address.
    For Solana addresses, validates the format and returns the original address.
    
    Args:
        address: The address string to validate
        
    Returns:
        The validated and formatted address
        
    Raises:
        ValueError: If the address is invalid for both Ethereum and Solana formats
    """
    # Try Ethereum address validation first
    try:
        return Web3.to_checksum_address(address)
    except (ValueError, TypeError):
        pass
    
    # Try Solana address validation
    try:
        # This will raise an exception if the address is invalid
        Pubkey.from_string(address)
        return address
    except Exception:
        pass
    
    raise ValueError(f"Invalid address format: {address}. Must be a valid Ethereum or Solana address.")


def is_ethereum_address(address: str) -> bool:
    """Check if an address is a valid Ethereum address."""
    try:
        Web3.to_checksum_address(address)
        return True
    except (ValueError, TypeError):
        return False


def is_solana_address(address: str) -> bool:
    """Check if an address is a valid Solana address."""
    try:
        Pubkey.from_string(address)
        return True
    except Exception:
        return False