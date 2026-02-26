import uuid

from fastapi import Depends, HTTPException, status

from src.interfaces.api_keys import (
    ApiKey,
    ApiKeyAdminListResponse,
    ApiKeyCreate,
    ApiKeyListResponse,
    ApiKeyType,
    ApiKeyUpdate,
    ChatApiKeyResponse,
    FullApiKey,
    ImageInferenceCallData,
    InferenceCallData,
)
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import SessionLocal
from src.routes.api_keys import router
from src.services.aleph import aleph_service
from src.services.api_key import ApiKeyService
from src.services.auth import get_current_address, verify_admin_token
from src.services.chat_request import ChatRequestService
from src.services.x402 import x402_service
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


@router.get("/chat")  # type: ignore
async def get_chat_api_key(current_address: str = Depends(get_current_address)) -> ChatApiKeyResponse:
    """
    Get the chat API key for the authenticated user.

    If the user doesn't have a chat API key, one will be automatically created.
    Returns only the full API key string.
    """
    try:
        chat_api_key = ApiKeyService.get_or_create_chat_api_key(address=current_address)
        return ChatApiKeyResponse(key=chat_api_key.full_key)
    except Exception as e:
        logger.error(f"Error getting or creating chat API key: {str(e)}", exc_info=True)
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
        if not existing_api_key.user_address or existing_api_key.user_address.lower() != current_address.lower():
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
            type=api_key.type,
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
        if not existing_api_key.user_address or existing_api_key.user_address.lower() != current_address.lower():
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


@router.post("/admin/usage")  # type: ignore
async def register_inference_call(usage_log: InferenceCallData) -> None:
    """Log API key usage.

    This endpoint is protected by admin authorization and requires
    the X-Admin-Token header to match the ADMIN_SECRET environment variable.

    For chat-type API keys, logs usage to chat_requests without deducting credits.
    For api-type API keys, logs usage to inference_calls and deducts credits.
    """

    try:
        # Check API key type
        with SessionLocal() as db:
            api_key = db.query(ApiKeyDB).filter(ApiKeyDB.key == usage_log.key).first()

            if not api_key:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found")

            # Handle based on API key type
            if api_key.type == ApiKeyType.chat:
                # For chat keys: log to chat_requests without deducting credits
                logger.debug(f"Logging chat request for key {usage_log.key}")
                if isinstance(usage_log, ImageInferenceCallData):
                    ChatRequestService.add_chat_request(
                        api_key_id=api_key.id,
                        input_tokens=0,
                        output_tokens=0,
                        cached_tokens=0,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                else:
                    ChatRequestService.add_chat_request(
                        api_key_id=api_key.id,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                        model_name=usage_log.model_name,
                    )
            elif api_key.type == ApiKeyType.liberclaw:
                # For liberclaw keys: log usage like API keys but skip credit deduction
                if isinstance(usage_log, ImageInferenceCallData):
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                    success = ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=credits_used,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                else:
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens - usage_log.cached_tokens,
                    )
                    success = ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=credits_used,
                        model_name=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                    )
                if not success:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found"
                    )
            elif api_key.type == ApiKeyType.x402:
                # For x402: calculate actual cost, settle via thirdweb, log usage
                if isinstance(usage_log, ImageInferenceCallData):
                    actual_cost = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                    ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=actual_cost,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                else:
                    actual_cost = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens - usage_log.cached_tokens,
                    )
                    ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=actual_cost,
                        model_name=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                    )

                # Settle payment via thirdweb
                if usage_log.payment_payload and usage_log.payment_requirements:
                    await x402_service.settle_payment(
                        usage_log.payment_payload,
                        usage_log.payment_requirements,
                    )

            else:
                # For API keys: calculate credits, log to inference_calls, and deduct credits
                # Determine if text or image based on type
                if isinstance(usage_log, ImageInferenceCallData):
                    # Image model
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                    logger.debug(f"Calculated {credits_used} credits for image model {usage_log.model_name}")

                    success = ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=credits_used,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                else:
                    # Text model (backward compatible)
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens - usage_log.cached_tokens,
                    )
                    logger.debug(f"Calculated {credits_used} credits for text model {usage_log.model_name}")

                    success = ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=credits_used,
                        model_name=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                    )

                if not success:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found"
                    )

        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging API key usage: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.get("/admin/list", dependencies=[Depends(verify_admin_token)])  # type: ignore
async def get_admin_all_api_keys() -> ApiKeyAdminListResponse:
    """
    Get all API keys across all addresses.

    This endpoint is protected by admin authorization and requires
    the X-Admin-Token header to match the ADMIN_SECRET environment variable.
    """
    try:
        api_keys = ApiKeyService.get_admin_all_api_keys()
        return ApiKeyAdminListResponse(keys=api_keys)
    except Exception as e:
        logger.error(f"Error getting all API keys: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")
