import json
import logging
import os

from dotenv import load_dotenv
from eth_typing import ChecksumAddress
from solders.solders import Pubkey
from web3 import Web3


class _Config:
    LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE: ChecksumAddress
    LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA: Pubkey
    BASE_RPC_URL: str
    SOLANA_RPC_URL: str

    DATABASE_URL: str

    THIRDWEB_WEBHOOK_SECRET: str

    LOG_LEVEL: int
    LOG_FILE: str | None

    JWT_SECRET: str
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int
    IS_DEVELOPMENT: bool

    ADMIN_SECRET: str
    VOUCHERS_PASSWORDS: list[str]

    def __init__(self):
        load_dotenv()
        self.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE = Web3.to_checksum_address(os.getenv("LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE"))
        self.LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA = Pubkey.from_string(os.getenv("LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA"))
        self.BASE_RPC_URL = os.getenv("BASE_RPC_URL")
        self.SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")

        self.DATABASE_URL = os.path.expandvars(os.getenv("DATABASE_URL", ""))

        self.THIRDWEB_WEBHOOK_SECRET = os.getenv("THIRDWEB_WEBHOOK_SECRET")

        # Configure logging
        log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
        self.LOG_LEVEL = getattr(logging, log_level_str, logging.INFO)
        self.LOG_FILE = os.getenv("LOG_FILE", None)

        self.JWT_SECRET = os.getenv("JWT_SECRET")
        self.JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES"))
        self.IS_DEVELOPMENT = os.getenv("IS_DEVELOPMENT", "False").lower() == "true"

        self.ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
        self.VOUCHERS_PASSWORDS = json.loads(os.environ["VOUCHERS_PASSWORDS"])


config = _Config()
