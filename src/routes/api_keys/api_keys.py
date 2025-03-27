from datetime import datetime

from fastapi import HTTPException, status

from src.interfaces.api_keys import (
    ApiKeyCreate,
    ApiKeyUpdate,
    ApiKeyUsageLog,
    ApiKeyResponse,
    ApiKeyListResponse,
    ApiKeyUsageResponse,
)
from src.services.api_key_service import ApiKeyService
from src.utils.logger import setup_logger
from . import router

logger = setup_logger(__name__)


@router.post("/api-keys/{address}")  # type: ignore
async def create_api_key(address: str, api_key_create: ApiKeyCreate) -> ApiKeyResponse:
    """Create a new API key for a user."""
    try:
        api_key = ApiKeyService.create_api_key(
            address=address, name=api_key_create.name, monthly_limit=api_key_create.monthly_limit
        )

        return ApiKeyResponse(
            key_id=api_key.key_id,
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


@router.get("/api-keys/{address}")  # type: ignore
async def get_api_keys(address: str) -> ApiKeyListResponse:
    """Get all API keys for a user."""
    try:
        api_keys = ApiKeyService.get_api_keys(address=address)

        return ApiKeyListResponse(
            keys=[
                ApiKeyResponse(
                    key_id=key.key_id,
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


@router.get("/api-keys/key/{key_id}")  # type: ignore
async def get_api_key(key_id: str) -> ApiKeyResponse:
    """Get a specific API key."""
    try:
        api_key = ApiKeyService.get_api_key(key_id=key_id)

        if not api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {key_id} not found")

        return ApiKeyResponse(
            key_id=api_key.key_id,
            name=api_key.name,
            address=api_key.address,
            created_at=api_key.created_at,
            is_active=api_key.is_active,
            monthly_limit=api_key.monthly_limit,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting API key: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error getting API key: {str(e)}"
        )


@router.put("/api-keys/key/{key_id}")  # type: ignore
async def update_api_key(key_id: str, api_key_update: ApiKeyUpdate) -> ApiKeyResponse:
    """Update an API key."""
    try:
        api_key = ApiKeyService.update_api_key(
            key_id=key_id,
            name=api_key_update.name,
            is_active=api_key_update.is_active,
            monthly_limit=api_key_update.monthly_limit,
        )

        if not api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {key_id} not found")

        return ApiKeyResponse(
            key_id=api_key.key_id,
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


@router.delete("/api-keys/key/{key_id}")  # type: ignore
async def delete_api_key(key_id: str) -> None:
    """Delete an API key."""
    try:
        success = ApiKeyService.delete_api_key(key_id=key_id)

        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {key_id} not found")

        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting API key: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error deleting API key: {str(e)}"
        )


@router.post("/api-keys/usage")  # type: ignore
async def log_api_key_usage(usage_log: ApiKeyUsageLog) -> None:
    """Log API key usage."""
    try:
        success = ApiKeyService.log_api_key_usage(
            key_id=usage_log.key_id,
            credits_used=usage_log.credits_used,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {usage_log.key_id} not found"
            )

        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging API key usage: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error logging API key usage: {str(e)}"
        )


@router.get("/api-keys/usage/{key_id}")  # type: ignore
async def get_api_key_usage_stats(
    key_id: str, start_date: datetime | None = None, end_date: datetime | None = None
) -> list[ApiKeyUsageResponse]:
    """Get API key usage statistics."""
    try:
        usages = ApiKeyService.get_api_key_usage_stats(key_id=key_id, start_date=start_date, end_date=end_date)

        return [
            ApiKeyUsageResponse(
                id=usage.id,
                key_id=usage.key_id,
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
