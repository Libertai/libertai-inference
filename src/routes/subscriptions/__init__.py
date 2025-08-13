from fastapi import APIRouter

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

from src.routes.subscriptions.subscriptions import cancel_subscription, get_subscription_by_user_address, get_subscription_transactions  # noqa
