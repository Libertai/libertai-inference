import uuid
from datetime import datetime
from http import HTTPStatus

from fastapi import Depends, HTTPException
from sqlalchemy import select

from src.interfaces.subscription import SubscriptionTransactionResponse, SubscriptionResponse
from src.models import SubscriptionStatus, Subscription, SubscriptionTransaction
from src.models.base import AsyncSessionLocal
from src.routes.subscriptions import router
from src.services.auth import get_current_address
from src.services.subscription import SubscriptionService
from src.utils.cron import scheduler
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.get("/{subscription_id}/transactions", description="Get subscription transactions")  # type: ignore
async def get_subscription_transactions(
    subscription_id: str, user_address: str = Depends(get_current_address)
) -> list[SubscriptionTransactionResponse]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Subscription).where(Subscription.id == subscription_id))
        subscription = result.scalars().first()
        if not subscription:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Subscription with ID {subscription_id} not found.",
            )
        if subscription.user_address != user_address:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=f"The subscription {subscription_id} does not belong to the user {user_address}.",
            )
        result = await db.execute(
            select(SubscriptionTransaction).where(SubscriptionTransaction.subscription_id == subscription_id)
        )
        subscription_transactions = result.scalars().all()
        return [SubscriptionTransactionResponse(**tx.__dict__) for tx in subscription_transactions]


@router.get("", description="Get subscriptions for the logged in user")  # type: ignore
async def get_subscription_by_user_address(
    user_address: str = Depends(get_current_address),
) -> list[SubscriptionResponse]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Subscription).where(Subscription.user_address == user_address))
        subscriptions = result.scalars().all()

        if not subscriptions:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"User {user_address} doesn't have subscriptions.",
            )
        return [SubscriptionResponse(**sub.__dict__) for sub in subscriptions]


@router.delete("/{subscription_id}", description="Cancel a subscription")  # type: ignore
async def cancel_subscription(
    subscription_id: uuid.UUID,
    user_address: str = Depends(get_current_address),
):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscription).where(Subscription.id == subscription_id, Subscription.user_address == user_address)
        )
        subscription = result.scalars().first()

        if not subscription:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Subscription with ID {subscription_id} not found.",
            )

        subscription.status = SubscriptionStatus.cancelled
        await db.commit()

    return {"details": "Subscription cancelled successfully"}


@scheduler.scheduled_job("interval", hours=1)
async def refresh_subscriptions() -> None:
    logger.info("Running scheduled subscription processing")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.active, Subscription.next_charge_at < datetime.now()
            )
        )
        active_subscriptions = result.scalars().all()
        logger.info(f"Processing {len(active_subscriptions)} active subscriptions due for renewal")

        for subscription in active_subscriptions:
            try:
                await SubscriptionService.process_renewal(subscription.id)
            except Exception as e:
                logger.error(f"Error processing subscription {subscription.id} renewal: {str(e)}", exc_info=True)

        result = await db.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.cancelled,
                Subscription.next_charge_at < datetime.now(),
            )
        )
        cancelled_subscriptions = result.scalars().all()

        logger.info(f"Processing {len(cancelled_subscriptions)} cancelled subscriptions now expired")
        for subscription in cancelled_subscriptions:
            try:
                subscription.status = SubscriptionStatus.inactive
            except Exception as e:
                logger.error(f"Error processing subscription {subscription.id} deactivation: {str(e)}", exc_info=True)
        await db.commit()

        logger.info("Subscriptions processing completed")
