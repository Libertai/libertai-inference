import uuid
from datetime import datetime

from dateutil.relativedelta import relativedelta

from src.models.base import SessionLocal
from src.models.subscription import Subscription, SubscriptionType, SubscriptionStatus
from src.models.subscription_transaction import SubscriptionTransaction, SubscriptionTransactionStatus
from src.services.credit import CreditService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class SubscriptionService:
    @staticmethod
    def create_subscription(
        user_address: str,
        subscription_type: SubscriptionType,
        amount: float,
        related_id: uuid.UUID,
        months: int = 1,
        db_session=None,
    ) -> Subscription:
        """
        Create a new subscription and process the initial payment.

        Args:
            user_address: User's blockchain address
            subscription_type: Type of subscription (agent, etc.)
            amount: Amount to charge per months
            related_id: ID of the related entity (agent_id, etc.)
            months: Number of months for the subscription (default is 1 month)
            db_session: Optional existing database session to use

        Returns:
            The newly created subscription
        """
        # Calculate the next charge date
        next_charge_at = datetime.now() + relativedelta(months=months)

        try:
            # Use provided session or create a new one
            if db_session:
                subscription = SubscriptionService._create_subscription_internal(
                    db_session, user_address, subscription_type, amount, related_id, next_charge_at
                )
                return subscription
            else:
                with SessionLocal() as db:
                    subscription = SubscriptionService._create_subscription_internal(
                        db, user_address, subscription_type, amount, related_id, next_charge_at
                    )
                    db.commit()  # Commit only if we created our own session
                    return subscription

        except Exception as e:
            logger.error(f"Error creating subscription for {user_address}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    def _create_subscription_internal(db, user_address, subscription_type, amount, related_id, next_charge_at):
        # Create the subscription
        subscription = Subscription(
            user_address=user_address,
            subscription_type=subscription_type,
            amount=amount,
            related_id=related_id,
            next_charge_at=next_charge_at,
            status=SubscriptionStatus.active,
        )
        db.add(subscription)

        # We need to flush so that the ID is generated but don't commit yet
        # if an external session was passed in
        db.flush()
        db.refresh(subscription)

        # Process initial payment
        if amount > 0:
            # Check balance
            balance = CreditService.get_balance(user_address)
            if balance < amount:
                raise ValueError(f"Insufficient balance: {balance} < {amount}. Subscription cannot be created.")

            # Deduct credits
            CreditService.use_credits(user_address, amount)

            # Create successful transaction record with the now-available subscription ID
            transaction = SubscriptionTransaction(
                subscription_id=subscription.id,
                amount=amount,
                status=SubscriptionTransactionStatus.success,
            )

            db.add(transaction)

        return subscription

    @staticmethod
    def process_renewal(subscription_id: uuid.UUID) -> bool:
        """
        Process a subscription renewal.

        Args:
            subscription_id: The ID of the subscription to renew

        Returns:
            Boolean indicating if the renewal was successful
        """
        try:
            with SessionLocal() as db:
                subscription = db.query(Subscription).filter(Subscription.id == subscription_id).first()

                if not subscription:
                    logger.warning(f"Subscription {subscription_id} not found")
                    return False

                if subscription.status != SubscriptionStatus.active:
                    logger.info(f"Subscription {subscription_id} is {subscription.status}, skipping renewal")
                    return False

                # Check if renewal is due
                now = datetime.now()
                if now < subscription.next_charge_at:
                    logger.debug(
                        f"Subscription {subscription_id} not due for renewal until {subscription.next_charge_at}"
                    )
                    return False

                # Check balance
                user_address = subscription.user_address
                amount = subscription.amount
                balance = CreditService.get_balance(user_address)

                if balance < amount:
                    # Create a failed transaction record
                    transaction = SubscriptionTransaction(
                        subscription_id=subscription.id,
                        amount=amount,
                        status=SubscriptionTransactionStatus.failed,
                        notes=f"Insufficient credits. Required: {amount}, Available: {balance}",
                    )
                    db.add(transaction)

                    subscription.status = SubscriptionStatus.inactive

                    db.commit()
                    logger.warning(f"Renewal failed for subscription {subscription_id}: Insufficient balance")
                    return False

                # Process payment
                CreditService.use_credits(user_address, amount)

                # Update subscription
                subscription.update_charge_dates(now)

                # Commit updates to subscription
                db.commit()
                db.refresh(subscription)

                # Create transaction record
                transaction = SubscriptionTransaction(
                    subscription_id=subscription.id,
                    amount=amount,
                    status=SubscriptionTransactionStatus.success,
                )
                db.add(transaction)

                db.commit()
                logger.info(f"Successfully renewed subscription {subscription_id}")
                return True

        except Exception as e:
            logger.error(f"Error processing renewal for subscription {subscription_id}: {str(e)}", exc_info=True)
            return False

    @staticmethod
    def cancel_subscription(subscription_id: uuid.UUID) -> bool:
        """
        Cancel a subscription.

        Args:
            subscription_id: The ID of the subscription to cancel

        Returns:
            Boolean indicating if the cancellation was successful
        """
        try:
            with SessionLocal() as db:
                subscription = db.query(Subscription).filter(Subscription.id == subscription_id).first()

                if not subscription:
                    logger.warning(f"Subscription {subscription_id} not found")
                    return False

                subscription.status = SubscriptionStatus.cancelled
                db.commit()

                logger.info(f"Cancelled subscription {subscription_id}")
                return True

        except Exception as e:
            logger.error(f"Error cancelling subscription {subscription_id}: {str(e)}", exc_info=True)
            return False

    @staticmethod
    def restart_subscription(subscription_id: uuid.UUID, months: int = 1) -> bool:
        """
        Restart an inactive subscription.

        Args:
            subscription_id: The ID of the subscription to restart
            months: Number of months for the subscription (default is 1 month)

        Returns:
            Boolean indicating if the operation was successful
        """
        try:
            with SessionLocal() as db:
                subscription = db.query(Subscription).filter(Subscription.id == subscription_id).first()

                if not subscription:
                    logger.warning(f"Subscription {subscription_id} not found")
                    return False

                if subscription.status != SubscriptionStatus.inactive:
                    logger.warning(f"Cannot resume non-inactive subscription {subscription_id}")
                    return False

                # Check balance
                user_address = subscription.user_address
                amount = subscription.amount
                balance = CreditService.get_balance(user_address)

                if balance < amount:
                    logger.warning(
                        f"Cannot restart subscription {subscription_id}: Insufficient balance ({balance} < {amount})"
                    )
                    return False

                # Process payment
                CreditService.use_credits(user_address, amount)

                # Update subscription
                subscription.status = SubscriptionStatus.active
                subscription.update_charge_dates(datetime.now(), months=months)

                # Commit updates to subscription
                db.commit()
                db.refresh(subscription)

                # Create transaction record
                transaction = SubscriptionTransaction(
                    subscription_id=subscription.id,
                    amount=amount,
                    status=SubscriptionTransactionStatus.success,
                    notes="Subscription resumed",
                )
                db.add(transaction)

                db.commit()

                logger.info(f"Restarted subscription {subscription_id}")
                # TODO: Call functions to restart related services (agents etc)
                return True

        except Exception as e:
            logger.error(f"Error restarting subscription {subscription_id}: {str(e)}", exc_info=True)
            return False
