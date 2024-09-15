from aleph.sdk import AlephHttpClient
from aleph.sdk.query.filters import PostFilter

from src.config import config
from src.interfaces.subscription import (
    SubscriptionType,
    SubscriptionDefinition,
    SubscriptionProvider,
    SubscriptionAccount,
    FetchedSubscription,
)


async def fetch_user_subscriptions(user_account: SubscriptionAccount) -> list[FetchedSubscription]:
    async with AlephHttpClient(api_server=config.ALEPH_API_URL) as client:
        result = await client.get_posts(
            post_filter=PostFilter(
                addresses=[config.SUBSCRIPTION_POST_SENDER],
                tags=[user_account.address],
                channels=[config.SUBSCRIPTION_POST_CHANNEL],
            )
        )
    return [FetchedSubscription(**post.content, hash=post.item_hash) for post in result.posts]


def find_subscription_group(subscription_type: SubscriptionType) -> list[SubscriptionDefinition] | None:
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

    sub_group_definitions = find_subscription_group(subscription_type)
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
            f"You can only have one active subscription at the same time between the following types: {[s.type for s in sub_group_definitions]}",
        )

    definition = next((sub_def for sub_def in sub_group_definitions if sub_def.type == subscription_type), None)
    if definition is None:
        return False, "Subscription definition not found"

    if provider not in definition.providers:
        return False, f"This subscription is only possible with providers {definition.providers}"

    same_existing_subscriptions = [sub for sub in active_subscriptions if sub.type == subscription_type]
    if len(same_existing_subscriptions) > 0 and not definition.multiple:
        return False, "You can only have one subscription of this type"

    return True, None
