import os

from dotenv import load_dotenv

from src.interfaces.subscription import SubscriptionDefinition, SubscriptionType, SubscriptionProvider


class _Config:
    ALEPH_API_URL: str | None
    LTAI_BALANCES_AGGREGATE_SENDER: str
    LTAI_BALANCES_AGGREGATE_KEY: str

    subscription_plans: list[list[SubscriptionDefinition]]

    def __init__(self):
        load_dotenv()

        self.ALEPH_API_URL = os.getenv("ALEPH_API_URL")
        self.LTAI_BALANCES_AGGREGATE_SENDER = os.getenv("LTAI_BALANCES_AGGREGATE_SENDER")
        self.LTAI_BALANCES_AGGREGATE_KEY = os.getenv("LTAI_BALANCES_AGGREGATE_KEY")

        self.subscription_plans = [
            [
                SubscriptionDefinition(
                    type=SubscriptionType.standard, providers=[SubscriptionProvider.hold], multiple=False
                )
            ]
        ]


config = _Config()
