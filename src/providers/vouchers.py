from http import HTTPStatus
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from libertai_utils.interfaces.subscription import (
    SubscriptionProvider,
    SubscriptionType,
    SubscriptionAccount,
    Subscription,
)

from src.interfaces.vouchers import (
    VouchersPostRefreshSubscriptionsResponse,
    VouchersPostSubscribeBody,
    VouchersPostSubscriptionResponse,
    VouchersCreatedSubscription,
    VouchersDeleteSubscribeBody,
    VouchersDeleteSubscriptionResponse,
)
from src.utils.general import get_current_time
from src.utils.subscription import (
    fetch_subscriptions,
    cancel_subscription,
    is_subscription_authorized,
    create_subscription,
)

router = APIRouter(prefix="/vouchers", tags=["Vouchers provider"])


@router.post("/subscription", description="Create one or multiple vouchers subscriptions")
async def subscribe(body: VouchersPostSubscribeBody) -> VouchersPostSubscriptionResponse:
    for subscription in body.subscriptions:
        # Looping to make all verifications before creating all the subscriptions at once
        existing_subscriptions = await fetch_subscriptions(addresses=[subscription.account.address])
        active_subscriptions = [sub for sub in existing_subscriptions if sub.is_active]

        # Checking if having this new subscription is possible with this provider and is compatible with the already active ones
        is_authorized, error = is_subscription_authorized(
            subscription.type, SubscriptionProvider.vouchers, active_subscriptions
        )
        if not is_authorized:
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN,
                detail=f"{subscription.account.address} - {subscription.type.value}: {error}",
            )

    # All subscriptions are authorized, let's process them
    created_subscriptions: list[VouchersCreatedSubscription] = []

    for subscription in body.subscriptions:
        subscription_to_create = __create_vouchers_subscription(
            subscription.type, subscription.account, subscription.end_time
        )
        post_message = await create_subscription(subscription_to_create)
        created_subscriptions.append(
            VouchersCreatedSubscription(
                account=subscription.account,
                end_time=subscription.end_time,
                type=subscription.type,
                post_hash=post_message.item_hash,
                subscription_id=subscription_to_create.id,
            )
        )
    return VouchersPostSubscriptionResponse(created_subscriptions=created_subscriptions)


@router.delete("/subscription", description="Stop some vouchers subscriptions")
async def cancel_vouchers_subscriptions(body: VouchersDeleteSubscribeBody) -> VouchersDeleteSubscriptionResponse:
    all_subscriptions = await fetch_subscriptions()
    active_vouchers_subscriptions = [
        sub for sub in all_subscriptions if sub.is_active and sub.provider == SubscriptionProvider.vouchers
    ]

    not_found_subscriptions: list[str] = []
    cancelled_subscriptions: list[str] = []

    for subscription_id in body.subscription_ids:
        subscription = next((sub for sub in active_vouchers_subscriptions if sub.id == subscription_id), None)
        if subscription is None:
            not_found_subscriptions.append(subscription_id)
            continue
        await cancel_subscription(subscription)
        cancelled_subscriptions.append(subscription_id)

    return VouchersDeleteSubscriptionResponse(
        not_found_subscriptions=not_found_subscriptions, cancelled_subscriptions=cancelled_subscriptions
    )


@router.post("/refresh", description="Check existing vouchers subscriptions to stop if the end_date is passed")
async def refresh_active_vouchers_subscriptions() -> VouchersPostRefreshSubscriptionsResponse:
    all_subscriptions = await fetch_subscriptions()
    active_vouchers_subscriptions = [
        sub for sub in all_subscriptions if sub.is_active and sub.provider == SubscriptionProvider.vouchers
    ]

    cancelled_subscriptions: list[str] = []
    current_time = get_current_time()

    for subscription in active_vouchers_subscriptions:
        # Checking if the subscription end date is passed
        if subscription.ended_at is not None and subscription.ended_at <= current_time:
            await cancel_subscription(subscription)
            cancelled_subscriptions.append(subscription.id)

    return VouchersPostRefreshSubscriptionsResponse(cancelled_subscriptions=cancelled_subscriptions)


def __create_vouchers_subscription(
    subscription_type: SubscriptionType, account: SubscriptionAccount, ended_at: int
) -> Subscription:
    subscription_id = str(uuid4())
    return Subscription(
        id=subscription_id,
        type=subscription_type,
        provider=SubscriptionProvider.vouchers,
        provider_data={},
        account=account,
        started_at=get_current_time(),
        ended_at=ended_at,
        is_active=True,
        tags=[account.address, subscription_id],
    )
