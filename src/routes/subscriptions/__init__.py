from fastapi import APIRouter

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

from src.routes.subscriptions.subscriptions import (  # noqa: E402
    cancel_subscription as cancel_subscription,
    get_subscription_by_user_address as get_subscription_by_user_address,
    get_subscription_transactions as get_subscription_transactions,
)
