from http import HTTPStatus

import aiohttp
from aleph.sdk import AlephHttpClient, AuthenticatedAlephHttpClient
from aleph.sdk.chains.ethereum import ETHAccount
from aleph.sdk.query.filters import PostFilter
from aleph_message.models import PostMessage
from libertai_utils.interfaces.agent import BaseSetupAgentBody, BaseDeleteAgentBody
from libertai_utils.interfaces.subscription import (
    SubscriptionType,
    SubscriptionDefinition,
    SubscriptionProvider,
    FetchedSubscription,
    Subscription,
)

from src.config import config
from src.utils.general import get_current_time


async def fetch_subscriptions(
    addresses: list[str] | None = None, subscription_ids: list[str] | None = None
) -> list[FetchedSubscription]:
    tags: list[str] | None = None
    if addresses is not None or subscription_ids is not None:
        tags = ([] if addresses is None else addresses) + ([] if subscription_ids is None else subscription_ids)

    async with AlephHttpClient(api_server=config.ALEPH_API_URL) as client:
        result = await client.get_posts(
            post_filter=PostFilter(
                types=[config.ALEPH_POST_TYPE],
                addresses=[config.ALEPH_OWNER],
                tags=tags,
                channels=[config.ALEPH_POST_CHANNEL],
            )
        )
    return [FetchedSubscription(**post.content, post_hash=post.original_item_hash) for post in result.posts]


def __find_subscription_group(subscription_type: SubscriptionType) -> list[SubscriptionDefinition] | None:
    for group in config.subscription_plans:
        found = any(plan for plan in group if plan.type == subscription_type)
        if found:
            return group
    return None


def is_subscription_authorized(
    subscription_type: SubscriptionType,
    provider: SubscriptionProvider,
    active_subscriptions: list[FetchedSubscription],
) -> tuple[bool, str | None]:
    """Check if adding this subscription is authorized with the ones already active"""

    sub_group_definitions = __find_subscription_group(subscription_type)
    if sub_group_definitions is None:
        return False, "Subscription group definition not found"

    other_group_definitions_sub_types = [
        sub_def.type for sub_def in sub_group_definitions if sub_def.type != subscription_type
    ]
    active_sub_types_in_same_group = [
        active_sub.type for active_sub in active_subscriptions if active_sub.type in other_group_definitions_sub_types
    ]
    if len(active_sub_types_in_same_group) > 1:
        # The user already has a subscription of another type within the same group
        return (
            False,
            f"You can only have one active subscription at the same time between the following types: {[s.type.value for s in sub_group_definitions]}",
        )

    definition = next((sub_def for sub_def in sub_group_definitions if sub_def.type == subscription_type), None)
    if definition is None:
        return False, "Subscription definition not found"

    if provider not in definition.providers:
        return (
            False,
            f"This subscription ({subscription_type.value}) is only possible with providers {definition.providers}",
        )

    same_existing_subscriptions = [sub for sub in active_subscriptions if sub.type == subscription_type]
    if len(same_existing_subscriptions) > 0 and not definition.multiple:
        return False, f"You can only have one subscription of this type ({subscription_type.value})"

    return True, None


async def create_subscription(subscription: Subscription) -> PostMessage:
    aleph_account = ETHAccount(config.ALEPH_POST_SENDER_SK)
    async with AuthenticatedAlephHttpClient(aleph_account, api_server=config.ALEPH_API_URL) as client:
        post_message, _status = await client.create_post(
            address=config.ALEPH_OWNER,
            post_content=subscription.dict(),
            post_type=config.ALEPH_POST_TYPE,
            channel=config.ALEPH_POST_CHANNEL,
        )

    if subscription.type == SubscriptionType.agent:
        # Launch the agent setup
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url=config.AGENTS_BACKEND_URL,
                json=BaseSetupAgentBody(
                    subscription_id=subscription.id,
                    account=subscription.account,
                    password=config.AGENTS_BACKEND_PASSWORD,
                ).dict(),
            ) as response:
                if response.status != HTTPStatus.OK:
                    # TODO: handle the error in some way
                    pass

    return post_message


async def cancel_subscription(subscription: FetchedSubscription) -> None:
    aleph_account = ETHAccount(config.ALEPH_POST_SENDER_SK)
    stopped_subscription = Subscription(
        **subscription.dict(exclude={"ended_at", "is_active"}), ended_at=get_current_time(), is_active=False
    )
    async with AuthenticatedAlephHttpClient(aleph_account, api_server=config.ALEPH_API_URL) as client:
        await client.create_post(
            address=config.ALEPH_OWNER,
            post_content=stopped_subscription.dict(),
            post_type="amend",
            ref=subscription.post_hash,
            channel=config.ALEPH_POST_CHANNEL,
        )

    if subscription.type == SubscriptionType.agent:
        # Launch the agent removal
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                url=config.AGENTS_BACKEND_URL,
                json=BaseDeleteAgentBody(
                    subscription_id=subscription.id, password=config.AGENTS_BACKEND_PASSWORD
                ).dict(),
            ) as response:
                if response.status != HTTPStatus.OK:
                    # TODO: handle the error in some way
                    pass
