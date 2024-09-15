import time
from http import HTTPStatus
from uuid import uuid4

from aleph.sdk import AlephHttpClient, AuthenticatedAlephHttpClient
from aleph.sdk.chains.ethereum import ETHAccount
from fastapi import APIRouter, HTTPException

from src.config import config
from src.interfaces.hold import (
    HoldPostSubscriptionBody,
    HoldAggregateData,
    HoldPostSubscriptionResponse,
    HoldDeleteSubscriptionBody,
    HoldDeleteSubscriptionResponse,
)
from src.interfaces.subscription import (
    SubscriptionType,
    Subscription,
    SubscriptionProvider,
    SubscriptionAccount,
)
from src.utils.subscription import is_subscription_authorized, fetch_user_subscriptions

router = APIRouter(tags=["Hold provider"])

# TODO: update these placeholder prices
ltai_hold_prices: dict[SubscriptionType, int] = {SubscriptionType.standard: 1000}


@router.post("/hold/subscription")
async def subscribe(body: HoldPostSubscriptionBody) -> HoldPostSubscriptionResponse:
    all_balances = await fetch_hold_balances()
    balance = all_balances.get(body.account.address, None)
    required_hold_amount = ltai_hold_prices.get(body.type, None)

    if balance is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail=f"Address {body.account.address} not found in holders list"
        )

    existing_subscriptions = await fetch_user_subscriptions(body.account)
    active_subscriptions = [sub for sub in existing_subscriptions if sub.is_active]
    active_hold_subscriptions = [sub for sub in active_subscriptions if sub.provider == SubscriptionProvider.hold]
    current_needed_holdings = sum([ltai_hold_prices.get(sub.type, 0) for sub in active_hold_subscriptions])

    is_authorized, error = is_subscription_authorized(body.type, SubscriptionProvider.hold, active_subscriptions)
    if not is_authorized:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=error)

    if required_hold_amount is None or (balance - current_needed_holdings) < required_hold_amount:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail=f"Not enough tokens held (balance {balance}, locked for existing subscriptions {current_needed_holdings}, available balance {balance - current_needed_holdings}, required {required_hold_amount})",
        )

    aleph_account = ETHAccount(config.SUBSCRIPTION_POST_SENDER_PK)
    subscription = create_hold_subscription(body.type, body.account)
    async with AuthenticatedAlephHttpClient(aleph_account, api_server=config.ALEPH_API_URL) as client:
        post_message, _status = await client.create_post(
            post_content=subscription.dict(),
            post_type=config.SUBSCRIPTION_POST_TYPE,
            channel=config.SUBSCRIPTION_POST_CHANNEL,
        )

    return HoldPostSubscriptionResponse(post_hash=post_message.item_hash, subscription_id=subscription.id)


@router.delete("/hold/subscription")
async def unsubscribe(body: HoldDeleteSubscriptionBody) -> HoldDeleteSubscriptionResponse:
    existing_subscriptions = await fetch_user_subscriptions(body.account)
    active_hold_subscriptions = [
        sub for sub in existing_subscriptions if sub.is_active and sub.provider == SubscriptionProvider.hold
    ]
    subscription = next((sub for sub in active_hold_subscriptions if sub.id == body.subscription_id), None)

    if subscription is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Subscription with ID {body.subscription_id} not found or not active",
        )

    aleph_account = ETHAccount(config.SUBSCRIPTION_POST_SENDER_PK)
    stopped_subscription = Subscription(
        **subscription.dict(exclude={"ended_at", "is_active"}), ended_at=int(time.time()), is_active=False
    )
    async with AuthenticatedAlephHttpClient(aleph_account, api_server=config.ALEPH_API_URL) as client:
        await client.create_post(
            post_content=stopped_subscription.dict(),
            post_type="amend",
            ref=subscription.post_hash,
            channel=config.SUBSCRIPTION_POST_CHANNEL,
        )
    return HoldDeleteSubscriptionResponse(success=True)


# TODO: CRON job to check active subscriptions and cancel them if not enough hold tokens


async def fetch_hold_balances() -> dict[str, int]:
    # TODO: use env server to fetch here
    async with AlephHttpClient() as client:
        result = await client.fetch_aggregates(
            address=config.LTAI_BALANCES_AGGREGATE_SENDER, keys=[config.LTAI_BALANCES_AGGREGATE_KEY]
        )
    balances = HoldAggregateData(tokens=result[config.LTAI_BALANCES_AGGREGATE_KEY])
    return {k.lower(): v for k, v in balances.tokens.items()}


def create_hold_subscription(subscription_type: SubscriptionType, account: SubscriptionAccount) -> Subscription:
    subscription_id = str(uuid4())
    return Subscription(
        id=subscription_id,
        type=subscription_type,
        provider=SubscriptionProvider.hold,
        account=account,
        started_at=int(time.time()),
        ended_at=None,
        is_active=True,
        tags=[account.address, subscription_id],
    )
