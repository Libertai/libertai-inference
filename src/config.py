import logging
import os

from dotenv import load_dotenv


class _Config:
    LTAI_BASE_ADDRESS: str
    LTAI_PAYMENT_PROCESSOR_CONTRACT: str
    DATABASE_URL: str

    LOG_LEVEL: int
    LOG_FILE: str | None

    def __init__(self):
        load_dotenv()
        self.LTAI_BASE_ADDRESS = os.getenv("LTAI_BASE_ADDRESS")
        self.LTAI_PAYMENT_PROCESSOR_CONTRACT = os.getenv("LTAI_PAYMENT_PROCESSOR_CONTRACT")
        self.DATABASE_URL = os.path.expandvars(os.getenv("DATABASE_URL", ""))

        # Configure logging
        log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
        self.LOG_LEVEL = getattr(logging, log_level_str, logging.INFO)
        self.LOG_FILE = os.getenv("LOG_FILE", None)


config = _Config()
