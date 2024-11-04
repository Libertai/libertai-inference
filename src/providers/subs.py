from http import HTTPStatus
from uuid import uuid4

import aiohttp
from fastapi import APIRouter, HTTPException
from libertai_utils.chains.ethereum import format_eth_address
from libertai_utils.interfaces.subscription import (
    SubscriptionProvider,
    SubscriptionType,
    SubscriptionAccount,
    Subscription,
    SubscriptionChain,
)

from src.config import config
from src.interfaces.subs import (
    SubsPostRefreshSubscriptionsResponse,
    SubsAPIGetSubscriptionsResponse,
    SubscriptionSubsProviderData,
)
from src.utils.general import get_current_time
from src.utils.subscription import fetch_subscriptions, cancel_subscription, create_subscription

router = APIRouter(prefix="/subs", tags=["Subs provider"])


@router.post("/refresh")
async def refresh() -> SubsPostRefreshSubscriptionsResponse:
    """Cancel existing unpaid subscriptions and creating newly paid ones"""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url=f"{config.SUBS_PROVIDER_CONFIG.api_url}/creator/subs",
            params={
                "appId": config.SUBS_PROVIDER_CONFIG.app_id,
                "chain": config.SUBS_PROVIDER_CONFIG.chain.value,
                "rpc": config.SUBS_PROVIDER_CONFIG.chain_rpc,
            },
            headers={"x-api-key": config.SUBS_PROVIDER_CONFIG.api_key},
        ) as response:
            data = await response.json()
            if response.status != HTTPStatus.OK:
                raise HTTPException(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    detail=f"Subs API returned a non-200 code: {data}",
                )
            if len(data) == 0:
                raise HTTPException(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR, detail=f"Subs API returned no data: {data}"
                )
            subs_subscriptions_data = [SubsAPIGetSubscriptionsResponse(**subscription) for subscription in data]

    all_subscriptions = await fetch_subscriptions()
    active_subscriptions = [sub for sub in all_subscriptions if sub.is_active]
    active_subs_subscriptions = [sub for sub in active_subscriptions if sub.provider == SubscriptionProvider.subs]

    created_subscriptions: list[str] = []
    cancelled_subscriptions: list[str] = []

    # Cancel existing active subscriptions if they aren't marked as active by Subs anymore
    for subscription in active_subs_subscriptions:
        subscription_provider_data = SubscriptionSubsProviderData(**subscription.provider_data)
        subs_subscription_data = next(
            (sub for sub in subs_subscriptions_data if sub.subsId == subscription_provider_data.subsId), None
        )
        if subs_subscription_data is None or not subs_subscription_data.active:
            # Subscription not found in Subs data or not active anymore
            await cancel_subscription(subscription)
            cancelled_subscriptions.append(subscription.id)

    # Create new subscriptions if marked as active in Subs but not by us
    subs_active_subscriptions_data = [sub for sub in subs_subscriptions_data if sub.active]
    for subs_active_subscription_data in subs_active_subscriptions_data:
        existing_subscription = next(
            (
                sub
                for sub in active_subs_subscriptions
                if SubscriptionSubsProviderData(**sub.provider_data).subsId == subs_active_subscription_data.subsId
            ),
            None,
        )
        if existing_subscription is not None:
            # Subscription already exist as active, nothing to do
            continue

        new_subscription = __create_subs_subscription(subs_active_subscription_data)
        await create_subscription(new_subscription)
        created_subscriptions.append(new_subscription.id)

    return SubsPostRefreshSubscriptionsResponse(
        created_subscriptions=created_subscriptions, cancelled_subscriptions=cancelled_subscriptions
    )


def __create_subs_subscription(subs_data: SubsAPIGetSubscriptionsResponse) -> Subscription:
    # TODO: find out from subs data the type of subscription
    subscription_type = SubscriptionType.pro
    account = SubscriptionAccount(address=format_eth_address(subs_data.payeeAddress), chain=SubscriptionChain.base)
    subscription_id = str(uuid4())

    return Subscription(
        id=subscription_id,
        type=subscription_type,
        provider=SubscriptionProvider.subs,
        provider_data=SubscriptionSubsProviderData(
            subsId=subs_data.subsId, tokenAddress=format_eth_address(subs_data.tokenAddress)
        ).dict(),
        account=account,
        started_at=get_current_time(),
        ended_at=None,
        is_active=True,
        tags=[account.address, subscription_id],
    )
