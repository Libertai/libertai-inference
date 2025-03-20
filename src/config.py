import os

from dotenv import load_dotenv


class _Config:
    LTAI_BASE_ADDRESS: str
    LTAI_PAYMENT_RECEIVER_ADDRESS: str

    def __init__(self):
        load_dotenv()
        self.LTAI_BASE_ADDRESS = os.getenv("LTAI_BASE_ADDRESS")
        self.LTAI_PAYMENT_RECEIVER_ADDRESS = os.getenv("LTAI_PAYMENT_RECEIVER_ADDRESS")


config = _Config()
