import time
from http import HTTPStatus
from uuid import uuid4

from aleph.sdk import AlephHttpClient, AuthenticatedAlephHttpClient
from aleph.sdk.chains.ethereum import ETHAccount
from aleph.sdk.query.filters import PostFilter
from fastapi import APIRouter, HTTPException

from src.config import config
from src.interfaces.hold import HoldPostSubscribeBody, HoldAggregateData
from src.interfaces.subscription import (
    SubscriptionType,
    Subscription,
    SUBSCRIPTION_VERSION,
    SubscriptionProvider,
    SubscriptionAccount,
)

router = APIRouter(tags=["Hold provider"])

# TODO: update these placeholder prices
ltai_hold_prices: dict[SubscriptionType, int] = {SubscriptionType.standard: 1000}


@router.post("/hold/subscribe")
async def subscribe(body: HoldPostSubscribeBody):
    all_balances = await fetch_hold_balances()
    address = body.account.address.upper()
    balance = all_balances.get(address, None)
    required_hold_amount = ltai_hold_prices.get(body.type, None)

    if balance is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail=f"Address {body.account.address} not found in holders list"
        )

    existing_subscriptions = await fetch_user_subscriptions(body.account)
    active_subscriptions = [sub for sub in existing_subscriptions if sub.is_active]
    active_hold_subscriptions = [sub for sub in active_subscriptions if sub.provider == SubscriptionProvider.hold]
    current_needed_holdings = sum([ltai_hold_prices.get(sub.type, 0) for sub in active_hold_subscriptions])

    if required_hold_amount is None or (balance - current_needed_holdings) < required_hold_amount:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail=f"Not enough tokens held (balance {balance}, locked for existing subscriptions {current_needed_holdings}, available balance {balance - current_needed_holdings}. required {required_hold_amount})",
        )

    return balance


# TODO: CRON job to check active subscriptions and cancel them if not enough hold tokens


async def fetch_hold_balances() -> dict[str, int]:
    # TODO: use dev server to fetch here
    async with AlephHttpClient() as client:
        result = await client.fetch_aggregates(
            address=config.LTAI_BALANCES_AGGREGATE_SENDER, keys=[config.LTAI_BALANCES_AGGREGATE_KEY]
        )
    balances = HoldAggregateData(tokens=result[config.LTAI_BALANCES_AGGREGATE_KEY])
    return {k.upper(): v for k, v in balances.tokens.items()}


async def fetch_user_subscriptions(user_account: SubscriptionAccount) -> list[Subscription]:
    aleph_account = ETHAccount(config.SUBSCRIPTION_POST_SENDER_PK)
    async with AuthenticatedAlephHttpClient(aleph_account, api_server=config.ALEPH_API_URL) as client:
        result = await client.get_posts(
            post_filter=PostFilter(
                addresses=[config.SUBSCRIPTION_POST_SENDER],
                tags=[user_account.address],
                channels=[config.SUBSCRIPTION_POST_CHANNEL],
            )
        )
        # TODO: add migrations here in case we change the stored format
        print(result)
    # TODO: return real subscriptions
    return []


async def create_hold_subscription(subscription_type: SubscriptionType, account: SubscriptionAccount) -> Subscription:
    return Subscription(
        version=SUBSCRIPTION_VERSION,
        id=str(uuid4()),
        type=subscription_type,
        provider=SubscriptionProvider.hold,
        account=account,
        started_at=int(time.time()),
        ended_at=None,
        is_active=True,
        tags=[account.address],
    )
