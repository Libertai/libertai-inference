import uuid
from datetime import datetime
from http import HTTPStatus

from fastapi import Depends, HTTPException

from src.models import SubscriptionStatus, Subscription
from src.models.base import SessionLocal
from src.routes.subscriptions import router
from src.services.auth import get_current_address
from src.services.subscription import SubscriptionService
from src.utils.cron import scheduler
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.delete("/{subscription_id}", description="Cancel a subscription")  # type: ignore
async def cancel_subscription(
    subscription_id: uuid.UUID,
    user_address: str = Depends(get_current_address),
):
    with SessionLocal() as db:
        subscription = (
            db.query(Subscription)
            .filter(Subscription.id == subscription_id, Subscription.user_address == user_address)
            .first()
        )

        if not subscription:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Subscription with ID {subscription_id} not found.",
            )

        success = SubscriptionService.cancel_subscription(subscription_id)

        if not success:
            raise HTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                detail="Failed to cancel the subscription.",
            )
        return {"details": "Subscription cancelled successfully"}


@scheduler.scheduled_job("interval", hours=1)
async def refresh_subscriptions() -> None:
    """
    Scheduled job to process all subscriptions.
    This includes:
    1. Processing renewals for active subscriptions
    2. Setting expired subscriptions to inactive status
    """
    logger.info("Running scheduled subscription processing")

    with SessionLocal() as db:
        # Find all active subscriptions due for renewal
        active_subscriptions: list[Subscription] = (
            db.query(Subscription)
            .filter(Subscription.status == SubscriptionStatus.active, Subscription.next_charge_at < datetime.now())
            .all()
        )
        logger.info(f"Processing {len(active_subscriptions)} active subscriptions due for renewal")

        # Process each subscription
        for subscription in active_subscriptions:
            try:
                # Process renewal
                SubscriptionService.process_renewal(subscription.id)
            except Exception as e:
                logger.error(f"Error processing subscription {subscription.id} renewal: {str(e)}", exc_info=True)

        cancelled_subscriptions = (
            db.query(Subscription)
            .filter(
                Subscription.status == SubscriptionStatus.cancelled,
                Subscription.next_charge_at < datetime.now(),
            )
            .all()
        )

        logger.info(f"Processing {len(cancelled_subscriptions)} cancelled subscriptions now expired")
        for subscription in cancelled_subscriptions:
            try:
                # Set subscription to inactive status
                subscription.status = SubscriptionStatus.inactive
            except Exception as e:
                logger.error(f"Error processing subscription {subscription.id} deactivation: {str(e)}", exc_info=True)
        db.commit()

        logger.info("Subscriptions processing completed")
