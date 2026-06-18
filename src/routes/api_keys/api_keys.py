import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select

from src.interfaces.api_keys import (
    ApiKey,
    ApiKeyAdminListResponse,
    ApiKeyCreate,
    ApiKeyListResponse,
    ApiKeyType,
    ApiKeyUpdate,
    ChatApiKeyResponse,
    CliApiKeyCreate,
    FullApiKey,
    ImageInferenceCallData,
    InferenceCallData,
)
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.routes.api_keys import router
from src.services.aleph import aleph_service
from src.services.api_key import ApiKeyService
from src.services.auth import get_current_user, verify_admin_token
from src.services.chat_request import ChatRequestService
from src.services.x402 import x402_service
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.post("")  # type: ignore
async def create_api_key(api_key_create: ApiKeyCreate, user: User = Depends(get_current_user)) -> FullApiKey:
    try:
        full_api_key = await ApiKeyService.create_api_key(
            user_id=user.id,
            name=api_key_create.name,
            monthly_limit=api_key_create.monthly_limit,
            user_address=user.address,
        )
        return full_api_key
    except Exception as e:
        logger.error(f"Error creating API key: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.get("")  # type: ignore
async def get_api_keys(user: User = Depends(get_current_user)) -> ApiKeyListResponse:
    try:
        api_keys = await ApiKeyService.get_api_keys(user_id=user.id)
        return ApiKeyListResponse(keys=api_keys)
    except Exception as e:
        logger.error(f"Error getting API keys: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.get("/chat")  # type: ignore
async def get_chat_api_key(user: User = Depends(get_current_user)) -> ChatApiKeyResponse:
    try:
        chat_api_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user.id, user_address=user.address)
        return ChatApiKeyResponse(key=chat_api_key.full_key)
    except Exception as e:
        logger.error(f"Error getting or creating chat API key: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.post("/cli")  # type: ignore
async def create_cli_api_key(
    cli_create: CliApiKeyCreate, user: User = Depends(get_current_user)
) -> FullApiKey:
    """Mint (or rotate in place) the CLI API key for the caller's device.

    Final step of the CLI browser-SSO login: the CLI calls this with the freshly
    exchanged session token. Returns the full key once (stored by the CLI).
    """
    try:
        return await ApiKeyService.rotate_or_create_cli_api_key(
            user_id=user.id, host=cli_create.host, user_address=user.address
        )
    except Exception as e:
        logger.error(f"Error creating CLI API key: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.get("/cli")  # type: ignore
async def get_cli_api_keys(user: User = Depends(get_current_user)) -> list[ApiKey]:
    try:
        return await ApiKeyService.get_cli_api_keys(user_id=user.id)
    except Exception as e:
        logger.error(f"Error getting CLI API keys: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")


@router.put("/{key_id}")  # type: ignore
async def update_api_key(
    key_id: uuid.UUID, api_key_update: ApiKeyUpdate, user: User = Depends(get_current_user)
) -> ApiKey:
    try:
        existing_api_key = await ApiKeyService.get_api_key_by_id(key_id=key_id)

        if not existing_api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {key_id} not found")

        if existing_api_key.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own API keys")

        api_key = await ApiKeyService.update_api_key(
            key_id=key_id,
            name=api_key_update.name,
            is_active=api_key_update.is_active,
            monthly_limit=api_key_update.monthly_limit,
        )

        if not api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {key_id} not found")

        return ApiKey(
            id=api_key.id,
            key=api_key.key,
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
async def delete_api_key(key_id: uuid.UUID, user: User = Depends(get_current_user)) -> None:
    try:
        existing_api_key = await ApiKeyService.get_api_key_by_id(key_id=key_id)

        if not existing_api_key:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key with ID {key_id} not found")

        if existing_api_key.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own API keys")

        success = await ApiKeyService.delete_api_key(key_id=key_id)

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
    """Usage report by bearer API key — meter one inference call against the key that made it.

    NOT an admin endpoint despite the ``/admin`` path prefix (legacy naming, kept only to
    avoid breaking the gateway that calls it). It intentionally takes NO admin token: the
    caller authenticates by *possessing* the user API key it reports usage for, which it
    sends as ``usage_log.key``. That key is the bearer credential.

    Security invariants this relies on (covered by tests/test_usage_report_auth.py):
      - An unknown key registers nothing and gets 404 — you cannot create or meter a key
        you don't already hold.
      - Only the supplied key is ever metered; there is no key/user parameter that would let
        a caller charge usage to a different key.

    An API key is unguessable (high-entropy secret), so possession is the authorization.
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ApiKeyDB).where(ApiKeyDB.key == usage_log.key))
            api_key = result.scalars().first()

            if not api_key:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found")

            if api_key.type == ApiKeyType.chat:
                logger.debug(f"Logging chat request for key {usage_log.key}")
                if isinstance(usage_log, ImageInferenceCallData):
                    await ChatRequestService.add_chat_request(
                        api_key_id=api_key.id,
                        input_tokens=0,
                        output_tokens=0,
                        cached_tokens=0,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                else:
                    await ChatRequestService.add_chat_request(
                        api_key_id=api_key.id,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                        model_name=usage_log.model_name,
                    )
            elif api_key.type == ApiKeyType.liberclaw:
                if isinstance(usage_log, ImageInferenceCallData):
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                    success = await ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=credits_used,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                else:
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                    )
                    success = await ApiKeyService.register_inference_call(
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
                if isinstance(usage_log, ImageInferenceCallData):
                    actual_cost = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                    await ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=actual_cost,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                else:
                    actual_cost = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                    )
                    await ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=actual_cost,
                        model_name=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                    )

                if usage_log.payment_payload and usage_log.payment_requirements:
                    await x402_service.settle_payment(
                        usage_log.payment_payload,
                        usage_log.payment_requirements,
                        actual_cost,
                    )

            else:
                if isinstance(usage_log, ImageInferenceCallData):
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                    logger.debug(f"Calculated {credits_used} credits for image model {usage_log.model_name}")

                    success = await ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=credits_used,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                    )
                else:
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                    )
                    logger.debug(f"Calculated {credits_used} credits for text model {usage_log.model_name}")

                    success = await ApiKeyService.register_inference_call(
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
    try:
        api_keys = await ApiKeyService.get_admin_all_api_keys()
        return ApiKeyAdminListResponse(keys=api_keys)
    except Exception as e:
        logger.error(f"Error getting all API keys: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"{str(e)}")
