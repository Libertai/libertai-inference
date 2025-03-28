from datetime import datetime

from fastapi import Depends, HTTPException, status

from src.interfaces.api_keys import (
    ApiKeyCreate,
    ApiKeyUpdate,
    ApiKeyUsageLog,
    ApiKeyResponse,
    ApiKeyListResponse,
    ApiKeyUsageResponse,
)
from src.routes.api_keys import router
from src.services.api_key import ApiKeyService
from src.services.auth import get_current_address
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.post("/{address}")  # type: ignore
async def create_api_key(
    address: str, api_key_create: ApiKeyCreate, current_address: str = Depends(get_current_address)
) -> ApiKeyResponse:
    """Create a new API key for a user."""
    # Verify that the requesting user is the address owner
    if current_address.lower() != address.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only manage API keys for your own address"
        )
    try:
        api_key = ApiKeyService.create_api_key(
            address=address, name=api_key_create.name, monthly_limit=api_key_create.monthly_limit
        )

        return ApiKeyResponse(
            key=api_key.key,
            name=api_key.name,
            address=api_key.address,
            created_at=api_key.created_at,
            is_active=api_key.is_active,
            monthly_limit=api_key.monthly_limit,
        )
    except Exception as e:
        logger.error(f"Error creating API key: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error creating API key: {str(e)}"
        )


@router.get("/{address}")  # type: ignore
async def get_api_keys(address: str, current_address: str = Depends(get_current_address)) -> ApiKeyListResponse:
    """Get all API keys for a user."""
    # Verify that the requesting user is the address owner
    if current_address.lower() != address.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only view API keys for your own address"
        )
    try:
        api_keys = ApiKeyService.get_api_keys(address=address)

        return ApiKeyListResponse(
            keys=[
                ApiKeyResponse(
                    key=key.key,
                    name=key.name,
                    address=key.address,
                    created_at=key.created_at,
                    is_active=key.is_active,
                    monthly_limit=key.monthly_limit,
                )
                for key in api_keys
            ]
        )
    except Exception as e:
        logger.error(f"Error getting API keys: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error getting API keys: {str(e)}"
        )


@router.put("/key/{key}")  # type: ignore
async def update_api_key(
    key: str, api_key_update: ApiKeyUpdate, current_address: str = Depends(get_current_address)
) -> ApiKeyResponse:
    """Update an API key."""
    try:
        # First, check if the API key exists and belongs to the authenticated user
        existing_api_key = ApiKeyService.get_api_key(key=key)

        if not existing_api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {key} not found")

        # Verify the API key belongs to the authenticated user
        if existing_api_key.address.lower() != current_address.lower():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own API keys")

        # Now update the API key
        api_key = ApiKeyService.update_api_key(
            key=key,
            name=api_key_update.name,
            is_active=api_key_update.is_active,
            monthly_limit=api_key_update.monthly_limit,
        )

        if not api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {key} not found")

        return ApiKeyResponse(
            key=api_key.key,
            name=api_key.name,
            address=api_key.address,
            created_at=api_key.created_at,
            is_active=api_key.is_active,
            monthly_limit=api_key.monthly_limit,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating API key: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error updating API key: {str(e)}"
        )


@router.delete("/key/{key}")  # type: ignore
async def delete_api_key(key: str, current_address: str = Depends(get_current_address)) -> None:
    """Delete an API key."""
    try:
        # First, check if the API key exists and belongs to the authenticated user
        existing_api_key = ApiKeyService.get_api_key(key=key)

        if not existing_api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {key} not found")

        # Verify the API key belongs to the authenticated user
        if existing_api_key.address.lower() != current_address.lower():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own API keys")

        # Now delete the API key
        success = ApiKeyService.delete_api_key(key=key)

        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {key} not found")

        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting API key: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error deleting API key: {str(e)}"
        )


@router.post("/usage")  # type: ignore
async def log_api_key_usage(usage_log: ApiKeyUsageLog) -> None:
    """Log API key usage."""

    # TODO: protect route to make it callable only by our models / load balancer
    try:
        # First, check if the API key exists and belongs to the authenticated user
        existing_api_key = ApiKeyService.get_api_key(key=usage_log.key)

        if not existing_api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found")

        # Now log the usage
        success = ApiKeyService.log_api_key_usage(
            key=usage_log.key,
            credits_used=usage_log.credits_used,
        )

        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found")

        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging API key usage: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error logging API key usage: {str(e)}"
        )


@router.get("/usage/{key}")  # type: ignore
async def get_api_key_usage_stats(
    key: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    current_address: str = Depends(get_current_address),
) -> list[ApiKeyUsageResponse]:
    """Get API key usage statistics."""
    try:
        # First, check if the API key exists and belongs to the authenticated user
        existing_api_key = ApiKeyService.get_api_key(key=key)

        if not existing_api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {key} not found")

        # Verify the API key belongs to the authenticated user
        if existing_api_key.address.lower() != current_address.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only view usage statistics for your own API keys",
            )

        # Now get the usage statistics
        usages = ApiKeyService.get_api_key_usage_stats(key=key, start_date=start_date, end_date=end_date)

        return [
            ApiKeyUsageResponse(
                id=usage.id,
                key=usage.key,
                credits_used=usage.credits_used,
                used_at=usage.used_at,
            )
            for usage in usages
        ]
    except Exception as e:
        logger.error(f"Error getting API key usage statistics: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting API key usage statistics: {str(e)}",
        )
