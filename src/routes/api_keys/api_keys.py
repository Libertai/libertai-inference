import uuid

from fastapi import Depends, HTTPException, status

from src.interfaces.api_keys import (
    ApiKeyCreate,
    ApiKeyUpdate,
    InferenceCallData,
    ApiKey,
    ApiKeyListResponse,
    FullApiKey,
)
from src.routes.api_keys import router
from src.services.api_key import ApiKeyService
from src.services.auth import get_current_address
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.post("")  # type: ignore
async def create_api_key(
    api_key_create: ApiKeyCreate, current_address: str = Depends(get_current_address)
) -> FullApiKey:
    """Create a new API key for a user."""
    try:
        # This is the only time the full key is returned
        full_api_key = ApiKeyService.create_api_key(
            address=current_address, name=api_key_create.name, monthly_limit=api_key_create.monthly_limit
        )

        return full_api_key
    except Exception as e:
        logger.error(f"Error creating API key: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.get("")  # type: ignore
async def get_api_keys(current_address: str = Depends(get_current_address)) -> ApiKeyListResponse:
    """Get all API keys for a user."""
    try:
        api_keys = ApiKeyService.get_api_keys(address=current_address)

        return ApiKeyListResponse(keys=api_keys)
    except Exception as e:
        logger.error(f"Error getting API keys: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.put("/{key_id}")  # type: ignore
async def update_api_key(
    key_id: uuid.UUID, api_key_update: ApiKeyUpdate, current_address: str = Depends(get_current_address)
) -> ApiKey:
    """Update an API key."""
    try:
        # First, check if the API key exists and belongs to the authenticated user
        existing_api_key = ApiKeyService.get_api_key_by_id(key_id=key_id)

        if not existing_api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {key_id} not found")

        # Verify the API key belongs to the authenticated user
        if existing_api_key.user_address.lower() != current_address.lower():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own API keys")

        # Now update the API key
        api_key = ApiKeyService.update_api_key(
            key_id=key_id,
            name=api_key_update.name,
            is_active=api_key_update.is_active,
            monthly_limit=api_key_update.monthly_limit,
        )

        if not api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {key_id} not found")

        return ApiKey(
            id=api_key.id,
            key=api_key.key,  # Already masked by the service
            name=api_key.name,
            user_address=api_key.user_address,
            created_at=api_key.created_at,
            is_active=api_key.is_active,
            monthly_limit=api_key.monthly_limit,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating API key: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.delete("/{key_id}")  # type: ignore
async def delete_api_key(key_id: uuid.UUID, current_address: str = Depends(get_current_address)) -> None:
    """Delete an API key."""
    try:
        # First, check if the API key exists and belongs to the authenticated user
        existing_api_key = ApiKeyService.get_api_key_by_id(key_id=key_id)

        if not existing_api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {key_id} not found")

        # Verify the API key belongs to the authenticated user
        if existing_api_key.user_address.lower() != current_address.lower():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own API keys")

        # Now delete the API key
        success = ApiKeyService.delete_api_key(key_id=key_id)

        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {key_id} not found")

        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting API key: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.post("/usage")  # type: ignore
async def register_inference_call(usage_log: InferenceCallData) -> None:
    """Log API key usage."""

    # TODO: protect route to make it callable only by our models / load balancer
    try:
        # Now log the usage
        success = ApiKeyService.register_inference_call(
            key=usage_log.key,
            credits_used=usage_log.credits_used,
            input_tokens=usage_log.input_tokens,
            output_tokens=usage_log.output_tokens,
            model_name=usage_log.model_name,
        )

        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found")

        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging API key usage: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")
