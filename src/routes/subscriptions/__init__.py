from fastapi import APIRouter

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

from src.routes.subscriptions.subscriptions import cancel_subscription  # noqa
