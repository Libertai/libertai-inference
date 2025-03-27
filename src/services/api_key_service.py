from datetime import datetime

from src.models.api_key import ApiKey
from src.models.api_key_usage import ApiKeyUsage
from src.models.base import SessionLocal
from src.models.user import User
from src.services.credit_service import CreditService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class ApiKeyService:
    @staticmethod
    def create_api_key(address: str, name: str, monthly_limit: float | None = None) -> ApiKey:
        """
        Create a new API key for a user.

        Args:
            address: User's blockchain address
            name: Name for the API key
            monthly_limit: Optional monthly usage limit in credits

        Returns:
            Newly created ApiKey object
        """
        logger.debug(f"Creating API key '{name}' for address {address}")
        db = SessionLocal()

        try:
            # Get or create user
            user = db.query(User).filter(User.address == address).first()
            if not user:
                user = User(address=address)
                db.add(user)
                db.flush()

            # Check if name already exists for this user
            existing_key = db.query(ApiKey).filter(ApiKey.address == address, ApiKey.name == name).first()

            if existing_key:
                logger.warning(f"API key with name '{name}' already exists for address {address}")
                db.rollback()
                return existing_key

            # Create new API key
            api_key = ApiKey(
                key_id=ApiKey.generate_key_id(),
                name=name,
                address=address,
                monthly_limit=monthly_limit,
            )
            db.add(api_key)
            db.commit()

            return api_key

        except Exception as e:
            db.rollback()
            logger.error(f"Error creating API key for {address}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def get_api_keys(address: str) -> list[ApiKey]:
        """
        Get all API keys for a user with usage statistics.

        Args:
            address: User's blockchain address

        Returns:
            List of ApiKey objects
        """
        logger.debug(f"Getting API keys for address {address}")
        db = SessionLocal()

        try:
            api_keys = db.query(ApiKey).filter(ApiKey.address == address).all()

            # Set session for property access
            for key in api_keys:
                setattr(key, "_session", db)

            return api_keys

        except Exception as e:
            logger.error(f"Error getting API keys for {address}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def get_api_key(key_id: str) -> ApiKey | None:
        """
        Get a specific API key by ID.

        Args:
            key_id: API key ID

        Returns:
            ApiKey object if found, None otherwise
        """
        logger.debug(f"Getting API key {key_id}")
        db = SessionLocal()

        try:
            api_key = db.query(ApiKey).filter(ApiKey.key_id == key_id).first()

            if api_key:
                setattr(api_key, "_session", db)

            return api_key

        except Exception as e:
            logger.error(f"Error getting API key {key_id}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def update_api_key(
        key_id: str,
        name: str | None = None,
        is_active: bool | None = None,
        monthly_limit: float | None = None,
    ) -> ApiKey | None:
        """
        Update an API key.

        Args:
            key_id: API key ID
            name: New name for the API key
            is_active: Whether the API key is active
            monthly_limit: Monthly usage limit in credits

        Returns:
            Updated ApiKey object if found, None otherwise
        """
        logger.debug(f"Updating API key {key_id}")
        db = SessionLocal()

        try:
            api_key = db.query(ApiKey).filter(ApiKey.key_id == key_id).first()

            if not api_key:
                logger.warning(f"API key {key_id} not found for update")
                return None

            # Update fields if provided
            if name is not None:
                # Check if name already exists for this user
                existing_key = (
                    db.query(ApiKey)
                    .filter(ApiKey.address == api_key.address, ApiKey.name == name, ApiKey.key_id != key_id)
                    .first()
                )

                if existing_key:
                    logger.warning(f"API key with name '{name}' already exists for address {api_key.address}")
                    db.rollback()
                    return None

                api_key.name = name

            if is_active is not None:
                api_key.is_active = is_active

            if monthly_limit is not None:
                api_key.monthly_limit = monthly_limit

            db.commit()

            setattr(api_key, "_session", db)
            return api_key

        except Exception as e:
            db.rollback()
            logger.error(f"Error updating API key {key_id}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def delete_api_key(key_id: str) -> bool:
        """
        Delete an API key.

        Args:
            key_id: API key ID

        Returns:
            Boolean indicating if the operation was successful
        """
        logger.debug(f"Deleting API key {key_id}")
        db = SessionLocal()

        try:
            api_key = db.query(ApiKey).filter(ApiKey.key_id == key_id).first()

            if not api_key:
                logger.warning(f"API key {key_id} not found for deletion")
                return False

            db.delete(api_key)
            db.commit()
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Error deleting API key {key_id}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def log_api_key_usage(key_id: str, credits_used: float) -> bool:
        """
        Log usage of an API key and deduct credits from the user's balance.
        This method is called after the actual API call has happened, so we only log
        usage and deduct credits without performing validation checks.

        Args:
            key_id: API key ID
            credits_used: Number of credits used

        Returns:
            Boolean indicating if the operation was successful
        """
        logger.debug(f"Logging usage of {credits_used} credits for API key {key_id}")
        db = SessionLocal()

        try:
            # Check if API key exists (even if inactive, we still want to log)
            api_key = db.query(ApiKey).filter(ApiKey.key_id == key_id).first()

            if not api_key:
                logger.warning(f"API key {key_id} not found")
                return False

            # Log usage regardless of balance or limits
            usage = ApiKeyUsage(key_id=key_id, credits_used=credits_used)
            db.add(usage)
            db.commit()

            # Deduct credits from user's balance
            # This may result in a negative balance, but that's handled by the credit service
            success = CreditService.use_credits(api_key.address, credits_used)

            if not success:
                logger.warning(f"Failed to deduct {credits_used} credits for API key {key_id}")

            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Error logging API key usage for {key_id}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def verify_api_key(key_id: str) -> ApiKey | None:
        """
        Verify that an API key exists and is active.

        Args:
            key_id: API key ID

        Returns:
            ApiKey object if valid, None otherwise
        """
        logger.debug(f"Verifying API key {key_id}")
        db = SessionLocal()

        try:
            api_key = (
                db.query(ApiKey)
                .filter(
                    ApiKey.key_id == key_id,
                    ApiKey.is_active == True,  # noqa: E712
                )
                .first()
            )

            if api_key:
                setattr(api_key, "_session", db)

            return api_key

        except Exception as e:
            logger.error(f"Error verifying API key {key_id}: {str(e)}", exc_info=True)
            return None
        finally:
            db.close()

    @staticmethod
    def get_api_key_usage_stats(
        key_id: str, start_date: datetime | None = None, end_date: datetime | None = None
    ) -> list[ApiKeyUsage]:
        """
        Get usage statistics for an API key.

        Args:
            key_id: API key ID
            start_date: Optional start date for filtering
            end_date: Optional end date for filtering

        Returns:
            List of ApiKeyUsage objects
        """
        logger.debug(f"Getting usage stats for API key {key_id}")
        db = SessionLocal()

        try:
            query = db.query(ApiKeyUsage).filter(ApiKeyUsage.key_id == key_id)

            if start_date:
                query = query.filter(ApiKeyUsage.used_at >= start_date)

            if end_date:
                query = query.filter(ApiKeyUsage.used_at <= end_date)

            return query.order_by(ApiKeyUsage.used_at.desc()).all()

        except Exception as e:
            logger.error(f"Error getting usage stats for API key {key_id}: {str(e)}", exc_info=True)
            return []
        finally:
            db.close()
