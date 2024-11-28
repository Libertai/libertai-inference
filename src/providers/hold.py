from http import HTTPStatus
from uuid import uuid4

from aleph.sdk import AlephHttpClient
from fastapi import APIRouter, HTTPException
from libertai_utils.chains.ethereum import format_eth_address
from libertai_utils.interfaces.subscription import (
    FetchedSubscription,
    Subscription,
    SubscriptionAccount,
    SubscriptionProvider,
    SubscriptionType,
)

from src.config import config
from src.interfaces.hold import (
    HoldAggregateData,
    HoldDeleteSubscriptionBody,
    HoldDeleteSubscriptionResponse,
    HoldGetMessagesResponse,
    HoldPostRefreshSubscriptionsResponse,
    HoldPostSubscriptionBody,
    HoldPostSubscriptionResponse,
)
from src.utils.general import get_current_time
from src.utils.signature import get_subscribe_message, get_unsubscribe_message
from src.utils.subscription import (
    cancel_subscription,
    create_subscription,
    fetch_subscriptions,
    is_subscription_authorized,
)

router = APIRouter(prefix="/hold", tags=["Hold provider"])

# TODO: update these placeholder prices
ltai_hold_prices: dict[SubscriptionType, int] = {SubscriptionType.pro: 1000, SubscriptionType.advanced: 2000}


@router.post("/subscription", description="Subscribe to a plan")
async def subscribe(body: HoldPostSubscriptionBody) -> HoldPostSubscriptionResponse:
    all_balances = await __fetch_hold_balances()
    balance = all_balances.get(body.account.address, None)
    required_hold_amount = ltai_hold_prices.get(body.type, None)

    if balance is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail=f"Address {body.account.address} not found in holders list"
        )

    existing_subscriptions = await fetch_subscriptions(addresses=[body.account.address])
    active_subscriptions = [sub for sub in existing_subscriptions if sub.is_active]
    active_hold_subscriptions = [sub for sub in active_subscriptions if sub.provider == SubscriptionProvider.hold]
    current_needed_holdings = sum([ltai_hold_prices.get(sub.type, 0) for sub in active_hold_subscriptions])

    # Checking if having this new subscription is possible with this provider and is compatible with the already active ones
    is_authorized, error = is_subscription_authorized(body.type, SubscriptionProvider.hold, active_subscriptions)
    if not is_authorized:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=error)

    # Checking if user has enough tokens available to subscribe
    if required_hold_amount is None or (balance - current_needed_holdings) < required_hold_amount:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail=f"Not enough tokens held (balance {balance}, locked for existing subscriptions {current_needed_holdings}, available balance {balance - current_needed_holdings}, required {required_hold_amount})",
        )

    # All good, creating the subscription
    subscription = __create_hold_subscription(body.type, body.account)
    post_message = await create_subscription(subscription)
    return HoldPostSubscriptionResponse(post_hash=post_message.item_hash, subscription_id=subscription.id)


@router.delete("/subscription", description="Unsubscribe of an existing subscription")
async def unsubscribe(body: HoldDeleteSubscriptionBody) -> HoldDeleteSubscriptionResponse:
    existing_subscriptions = await fetch_subscriptions(addresses=[body.account.address])
    active_hold_subscriptions = [
        sub for sub in existing_subscriptions if sub.is_active and sub.provider == SubscriptionProvider.hold
    ]
    subscription = next((sub for sub in active_hold_subscriptions if sub.id == body.subscription_id), None)

    if subscription is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Subscription with ID {body.subscription_id} not found or not active",
        )

    await cancel_subscription(subscription)
    return HoldDeleteSubscriptionResponse(success=True)


# TODO: add cron job when live
@router.post(
    "/refresh", description="Delete existing active hold subscriptions if not enough tokens held in the wallet"
)
async def refresh_active_hold_subscriptions() -> HoldPostRefreshSubscriptionsResponse:
    all_balances = await __fetch_hold_balances()
    all_subscriptions = await fetch_subscriptions()
    active_hold_subscriptions = [
        sub for sub in all_subscriptions if sub.is_active and sub.provider == SubscriptionProvider.hold
    ]
    cancelled_subscriptions: list[str] = []

    # Grouping active subscription per address (aka user)
    subscriptions_per_address: dict[str, list[FetchedSubscription]] = {}
    for subscription in active_hold_subscriptions:
        user_subscriptions = subscriptions_per_address.get(subscription.account.address, [])
        user_subscriptions.append(subscription)
        subscriptions_per_address[subscription.account.address] = user_subscriptions

    # Checking if each user still holds enough tokens for his current active subscriptions
    for address, subscriptions in subscriptions_per_address.items():
        balance = all_balances.get(address, 0)
        required_hold_amount = sum([ltai_hold_prices.get(sub.type, 0) for sub in subscriptions])

        # Cancelling subscriptions until the balance is high enough for the subscriptions left (if any)
        i = 0
        while balance < required_hold_amount:
            subscription_to_cancel = subscriptions[i]
            await cancel_subscription(subscription_to_cancel)
            cancelled_subscriptions.append(subscription_to_cancel.id)
            required_hold_amount -= ltai_hold_prices.get(subscription_to_cancel.type, 0)
            i += 1

    return HoldPostRefreshSubscriptionsResponse(cancelled_subscriptions=cancelled_subscriptions)


@router.get("/message", description="Returns the messages to sign to authenticate other actions")
def hold_subscription_messages(subscription_type: SubscriptionType) -> HoldGetMessagesResponse:
    return HoldGetMessagesResponse(
        subscribe_message=get_subscribe_message(subscription_type, SubscriptionProvider.hold),
        unsubscribe_message=get_unsubscribe_message(subscription_type, SubscriptionProvider.hold),
    )


async def __fetch_hold_balances() -> dict[str, int]:
    async with AlephHttpClient(api_server=config.ALEPH_API_URL) as client:
        result = await client.fetch_aggregate(
            address=config.LTAI_BALANCES_AGGREGATE_SENDER, key=config.LTAI_BALANCES_AGGREGATE_KEY
        )
    balances = HoldAggregateData(tokens=result)
    return {format_eth_address(k): v for k, v in balances.tokens.items()}


def __create_hold_subscription(subscription_type: SubscriptionType, account: SubscriptionAccount) -> Subscription:
    subscription_id = str(uuid4())
    return Subscription(
        id=subscription_id,
        type=subscription_type,
        provider=SubscriptionProvider.hold,
        provider_data={},
        account=account,
        started_at=get_current_time(),
        ended_at=None,
        is_active=True,
        tags=[account.address, subscription_id],
    )
