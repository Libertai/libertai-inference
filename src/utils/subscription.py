from aleph.sdk import AlephHttpClient, AuthenticatedAlephHttpClient
from aleph.sdk.chains.ethereum import ETHAccount
from aleph.sdk.query.filters import PostFilter
from aleph_message.models import PostMessage

from src.config import config
from src.interfaces.subscription import (
    SubscriptionType,
    SubscriptionDefinition,
    SubscriptionProvider,
    FetchedSubscription,
    Subscription,
)
from src.utils.general import get_current_time


async def fetch_subscriptions(addresses: list[str] | None = None) -> list[FetchedSubscription]:
    async with AlephHttpClient(api_server=config.ALEPH_API_URL) as client:
        result = await client.get_posts(
            post_filter=PostFilter(
                addresses=[config.SUBSCRIPTION_POST_SENDER],
                tags=addresses,
                channels=[config.SUBSCRIPTION_POST_CHANNEL],
            )
        )
    return [FetchedSubscription(**post.content, post_hash=post.item_hash) for post in result.posts]


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
    aleph_account = ETHAccount(config.SUBSCRIPTION_POST_SENDER_SK)
    async with AuthenticatedAlephHttpClient(aleph_account, api_server=config.ALEPH_API_URL) as client:
        post_message, _status = await client.create_post(
            post_content=subscription.dict(),
            post_type=config.SUBSCRIPTION_POST_TYPE,
            channel=config.SUBSCRIPTION_POST_CHANNEL,
        )

    return post_message


async def cancel_subscription(subscription: FetchedSubscription):
    aleph_account = ETHAccount(config.SUBSCRIPTION_POST_SENDER_SK)
    stopped_subscription = Subscription(
        **subscription.dict(exclude={"ended_at", "is_active"}), ended_at=get_current_time(), is_active=False
    )
    async with AuthenticatedAlephHttpClient(aleph_account, api_server=config.ALEPH_API_URL) as client:
        await client.create_post(
            post_content=stopped_subscription.dict(),
            post_type="amend",
            ref=subscription.post_hash,
            channel=config.SUBSCRIPTION_POST_CHANNEL,
        )
