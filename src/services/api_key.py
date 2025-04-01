import uuid

from src.interfaces.api_keys import FullApiKey, ApiKey
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import SessionLocal
from src.models.inference_call import InferenceCall
from src.models.user import User
from src.services.credit import CreditService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class ApiKeyService:
    @staticmethod
    def create_api_key(address: str, name: str, monthly_limit: float | None = None) -> FullApiKey:
        """
        Create a new API key for a user.

        Args:
            address: User's blockchain address
            name: Name for the API key
            monthly_limit: Optional monthly usage limit in credits

        Returns:
            Newly created ApiKey object with all properties eagerly loaded
            This is the only time the FULL key is returned
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
            existing_key = db.query(ApiKeyDB).filter(ApiKeyDB.user_address == address, ApiKeyDB.name == name).first()

            if existing_key:
                db.rollback()
                raise ValueError(f"API key with name '{name}' already exists")

            # Create new API key
            key = ApiKeyDB.generate_key()
            api_key = ApiKeyDB(
                key=key,
                name=name,
                user_address=address,
                monthly_limit=monthly_limit,
            )
            db.add(api_key)
            db.commit()

            # Create a clean detached copy of the object with all required attributes
            # For newly created keys, we DO want to return the full key

            return FullApiKey(
                id=api_key.id,
                key=api_key.masked_key,
                full_key=key,
                name=name,
                user_address=api_key.user_address,
                created_at=api_key.created_at,
                is_active=api_key.is_active,
                monthly_limit=api_key.monthly_limit,
            )

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
            List of ApiKey objects with all properties eagerly loaded
            Keys are masked for security
        """
        logger.debug(f"Getting API keys for address {address}")
        db = SessionLocal()

        try:
            api_keys = db.query(ApiKeyDB).filter(ApiKeyDB.user_address == address).all()

            # Create fully detached copies
            result = []
            for key in api_keys:
                # Create a detached copy with all needed attributes
                detached_key = ApiKey(
                    key=key.masked_key,  # Masked key for display
                    name=key.name,
                    user_address=key.user_address,
                    monthly_limit=key.monthly_limit,
                    id=key.id,
                    created_at=key.created_at,
                    is_active=key.is_active,
                )
                result.append(detached_key)

            return result

        except Exception as e:
            logger.error(f"Error getting API keys for {address}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def get_api_key_by_id(key_id: uuid.UUID) -> ApiKey | None:
        """
        Get a specific API key by ID.

        Args:
            key_id: API key UUID

        Returns:
            ApiKey object if found, None otherwise
            Key is masked for security
        """
        logger.debug(f"Getting API key with ID {key_id}")
        db = SessionLocal()

        try:
            api_key = db.query(ApiKeyDB).filter(ApiKeyDB.id == key_id).first()

            if not api_key:
                return None

            return ApiKey(
                key=api_key.masked_key,  # Masked key for display
                name=api_key.name,
                user_address=api_key.user_address,
                monthly_limit=api_key.monthly_limit,
                id=api_key.id,
                created_at=api_key.created_at,
                is_active=api_key.is_active,
            )

        except Exception as e:
            logger.error(f"Error getting API key with ID {key_id}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def update_api_key(
        key_id: uuid.UUID,
        name: str | None = None,
        is_active: bool | None = None,
        monthly_limit: float | None = None,
    ) -> ApiKey | None:
        """
        Update an API key.

        Args:
            key_id: API key UUID
            name: New name for the API key
            is_active: Whether the API key is active
            monthly_limit: Monthly usage limit in credits

        Returns:
            Updated ApiKey object if found, None otherwise
            Key is masked for security
        """
        logger.debug(f"Updating API key {key_id}")
        db = SessionLocal()

        try:
            api_key = db.query(ApiKeyDB).filter(ApiKeyDB.id == key_id).first()

            if not api_key:
                logger.warning(f"API key {key_id} not found for update")
                return None

            # Update fields if provided
            if name is not None:
                # Check if name already exists for this user
                existing_key = (
                    db.query(ApiKeyDB)
                    .filter(
                        ApiKeyDB.user_address == api_key.user_address, ApiKeyDB.name == name, ApiKeyDB.id != key_id
                    )
                    .first()
                )

                if existing_key:
                    logger.warning(f"API key with name '{name}' already exists for address {api_key.user_address}")
                    db.rollback()
                    return None

                api_key.name = name

            if is_active is not None:
                api_key.is_active = is_active

            if monthly_limit is not None:
                api_key.monthly_limit = monthly_limit

            db.commit()

            return ApiKey(
                key=api_key.masked_key,  # Masked key for display
                name=api_key.name,
                user_address=api_key.user_address,
                monthly_limit=api_key.monthly_limit,
                id=api_key.id,
                created_at=api_key.created_at,
                is_active=api_key.is_active,
            )

        except Exception as e:
            db.rollback()
            logger.error(f"Error updating API key {key_id}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()

    @staticmethod
    def delete_api_key(key_id: uuid.UUID) -> bool:
        """
        Delete an API key.

        Args:
            key_id: API key UUID

        Returns:
            Boolean indicating if the operation was successful
        """
        logger.debug(f"Deleting API key {key_id}")
        db = SessionLocal()

        try:
            api_key = db.query(ApiKeyDB).filter(ApiKeyDB.id == key_id).first()

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
    def get_all_api_keys() -> list[ApiKey]:
        """
        Get all API keys across all addresses.
        This method is intended for admin use only.

        Returns:
            List of ApiKey objects for all users with all properties eagerly loaded
            Keys are masked for security
        """
        logger.debug("Getting all API keys (admin request)")
        db = SessionLocal()

        try:
            api_keys = db.query(ApiKeyDB).all()

            # Create fully detached copies
            result = []
            for key in api_keys:
                # Create a detached copy with all needed attributes
                detached_key = ApiKey(
                    key=key.masked_key,  # Masked key for display
                    name=key.name,
                    user_address=key.user_address,
                    monthly_limit=key.monthly_limit,
                    id=key.id,
                    created_at=key.created_at,
                    is_active=key.is_active,
                )
                result.append(detached_key)

            return result

        except Exception as e:
            logger.error(f"Error getting all API keys: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()
    
    @staticmethod
    def register_inference_call(
        key: str, credits_used: float, input_tokens: int, output_tokens: int, model_name: str
    ) -> bool:
        """
        Log usage of an API key and deduct credits from the user's balance.
        This method is called after the actual API call has happened, so we only log
        usage and deduct credits without performing validation checks.

        Args:
            key: API key string
            credits_used: Number of credits used
            input_tokens: Number of input tokens processed
            output_tokens: Number of output tokens generated
            model_name: Name of the model used

        Returns:
            Boolean indicating if the operation was successful
        """
        logger.debug(f"Logging usage of {credits_used} credits for API key {key}")
        db = SessionLocal()

        try:
            # Check if API key exists (even if inactive, we still want to log)
            api_key = db.query(ApiKeyDB).filter(ApiKeyDB.key == key).first()

            if not api_key:
                logger.warning(f"API key {key} not found")
                return False

            # Log usage with the API key ID
            usage = InferenceCall(
                api_key_id=api_key.id,
                credits_used=credits_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_name=model_name,
            )
            db.add(usage)
            db.commit()

            # Deduct credits from user's balance
            # This may result in a negative balance, but that's handled by the credit service
            success = CreditService.use_credits(api_key.user_address, credits_used)

            if not success:
                logger.warning(f"Failed to deduct {credits_used} credits for API key {key}")

            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Error logging API key usage for {key}: {str(e)}", exc_info=True)
            raise e
        finally:
            db.close()
