import json
import os

from dotenv import load_dotenv
from libertai_utils.interfaces.subscription import SubscriptionDefinition, SubscriptionType, SubscriptionProvider

from src.interfaces.subs import SubsConfig, SubsChain


class _Config:
    ALEPH_API_URL: str | None
    LTAI_BALANCES_AGGREGATE_SENDER: str
    LTAI_BALANCES_AGGREGATE_KEY: str

    ALEPH_POST_SENDER: str
    ALEPH_OWNER: str
    ALEPH_POST_SENDER_SK: bytes
    ALEPH_POST_CHANNEL: str
    ALEPH_POST_TYPE: str

    VOUCHERS_PASSWORDS: list[str]

    AGENTS_BACKEND_URL: str
    AGENTS_BACKEND_PASSWORD: str

    SUBS_PROVIDER_CONFIG: SubsConfig

    subscription_plans: list[list[SubscriptionDefinition]]

    def __init__(self):
        load_dotenv()

        self.ALEPH_API_URL = os.getenv("ALEPH_API_URL")
        self.LTAI_BALANCES_AGGREGATE_SENDER = os.getenv("LTAI_BALANCES_AGGREGATE_SENDER")
        self.LTAI_BALANCES_AGGREGATE_KEY = os.getenv("LTAI_BALANCES_AGGREGATE_KEY")
        self.ALEPH_POST_SENDER = os.getenv("ALEPH_POST_SENDER")
        self.ALEPH_OWNER = os.getenv("ALEPH_OWNER")
        self.ALEPH_POST_SENDER_SK = os.getenv("ALEPH_POST_SENDER_SK")  # type: ignore
        self.ALEPH_POST_CHANNEL = os.getenv("ALEPH_POST_CHANNEL", "libertai")
        self.ALEPH_POST_TYPE = os.getenv("ALEPH_POST_TYPE", "libertai-subscription")

        self.VOUCHERS_PASSWORDS = json.loads(os.environ["VOUCHERS_PASSWORDS"])

        self.AGENTS_BACKEND_URL = os.getenv("AGENTS_BACKEND_URL")
        self.AGENTS_BACKEND_PASSWORD = os.getenv("AGENTS_BACKEND_PASSWORD")

        self.SUBS_PROVIDER_CONFIG = SubsConfig(
            api_url=os.getenv("SUBS_API_URL", "https://api.subsprotocol.com"),
            api_key=os.getenv("SUBS_API_KEY"),
            chain=os.getenv("SUBS_CHAIN", SubsChain.bsc),
            chain_rpc=os.getenv("SUBS_CHAIN_RPC", "https://binance.llamarpc.com"),
            app_id=os.getenv("SUBS_APP_ID", 6),
        )

        self.subscription_plans = [
            [
                SubscriptionDefinition(
                    type=SubscriptionType.pro,
                    providers=[SubscriptionProvider.hold, SubscriptionProvider.subs, SubscriptionProvider.vouchers],
                ),
                SubscriptionDefinition(
                    type=SubscriptionType.advanced,
                    providers=[SubscriptionProvider.hold, SubscriptionProvider.subs, SubscriptionProvider.vouchers],
                ),
            ],
            [
                SubscriptionDefinition(
                    type=SubscriptionType.agent, providers=[SubscriptionProvider.vouchers], multiple=True
                )
            ],
        ]


config = _Config()
