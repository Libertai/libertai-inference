import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select

from src.config import config
from src.interfaces.api_keys import (
    ApiKey,
    ApiKeyAdminListResponse,
    ApiKeyCreate,
    ApiKeyListResponse,
    ApiKeyType,
    ApiKeyUpdate,
    ChatApiKeyResponse,
    CliApiKey,
    CliApiKeyCreate,
    FullApiKey,
    ImageInferenceCallData,
    InferenceCallData,
    InferenceCallResponse,
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
    except Exception:
        logger.error("Error creating API key", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create API key")


@router.get("")  # type: ignore
async def get_api_keys(user: User = Depends(get_current_user)) -> ApiKeyListResponse:
    try:
        api_keys = await ApiKeyService.get_api_keys(user_id=user.id)
        return ApiKeyListResponse(keys=api_keys)
    except Exception as e:
        logger.error(f"Error getting API keys: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/chat")  # type: ignore
async def get_chat_api_key(user: User = Depends(get_current_user)) -> ChatApiKeyResponse:
    try:
        chat_api_key = await ApiKeyService.get_or_create_chat_api_key(user_id=user.id, user_address=user.address)
        return ChatApiKeyResponse(key=chat_api_key.full_key)
    except Exception as e:
        logger.error(f"Error getting or creating chat API key: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post("/cli")  # type: ignore
async def create_cli_api_key(cli_create: CliApiKeyCreate, user: User = Depends(get_current_user)) -> FullApiKey:
    """Mint (or rotate in place) the CLI API key for the caller's device.

    Final step of the CLI browser-SSO login: the CLI calls this with the freshly
    exchanged session token. Returns the full key once (stored by the CLI).
    """
    try:
        return await ApiKeyService.rotate_or_create_cli_api_key(
            user_id=user.id, host=cli_create.host, user_address=user.address
        )
    except Exception as e:
        logger.error(f"Error creating CLI API key: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/cli")  # type: ignore
async def get_cli_api_keys(user: User = Depends(get_current_user)) -> list[CliApiKey]:
    try:
        return await ApiKeyService.get_cli_api_keys(user_id=user.id)
    except Exception as e:
        logger.error(f"Error getting CLI API keys: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


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
            updates=api_key_update.model_dump(exclude_unset=True, include={"name", "is_active", "monthly_limit"}),
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
        logger.error(f"Error updating API key: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


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

        return
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting API key: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post("/admin/usage")  # type: ignore
async def register_inference_call(usage_log: InferenceCallData) -> InferenceCallResponse:
    """Usage report by bearer API key — meter one inference call against the key that made it.

    The response says whether the key is still usable now that this call is metered, so the
    reporting model server can evict a key that just ran out instead of serving it until the
    next whitelist push.

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

    async def _response() -> InferenceCallResponse:
        """Key-usability hint after metering. The report already persisted by the time this
        runs, and it is advisory — so a failure reading it must not 500 a committed report:
        the gateway's retry would then insert a second usage row.

        On error the key is reported as usable (invalid=None): a key that just ran out may
        then keep being served until the next whitelist push. That is the deliberate trade-
        off — failing closed would spuriously evict healthy keys on transient read errors,
        which is worse for an advisory field."""
        try:
            return InferenceCallResponse(invalid=await ApiKeyService.get_invalid_key_info(usage_log.key))
        except Exception as e:
            logger.error(f"Error checking key usability after metering: {e!s}", exc_info=True)
            return InferenceCallResponse(invalid=None)

    try:
        # Settled after the session block releases its pooled connection: settlement is
        # an external HTTP call (blockchain confirmation can take seconds) that must
        # not hold one.
        x402_settlement: tuple[str, str | None, str | None, float] | None = None

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(ApiKeyDB).where(ApiKeyDB.key == usage_log.key))
            api_key = result.scalars().first()

            if not api_key:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found")

            # Attribute reads (masked_key, type) are hoisted before the commit so the
            # route never depends on the sessionmaker's expire_on_commit=False.
            key_type = api_key.type

            if key_type == ApiKeyType.chat:
                # The shared anonymous chat key stays free; per-user chat keys are metered
                # (window -> prepaid) via register_inference_call, like api/cli keys.
                if not (config.LIBERTAI_CHAT_API_KEY and usage_log.key == config.LIBERTAI_CHAT_API_KEY):
                    if isinstance(usage_log, ImageInferenceCallData):
                        credits_used = await aleph_service.calculate_price(
                            model_id=usage_log.model_name, image_count=usage_log.image_count
                        )
                        success = await ApiKeyService.register_inference_call(
                            key=usage_log.key,
                            credits_used=credits_used,
                            model_name=usage_log.model_name,
                            image_count=usage_log.image_count,
                            db=db,
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
                            db=db,
                        )
                    if not success:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found"
                        )
                # The chat-history log trails the metering: a call the billing path refuses
                # must not leave a row behind in the history the stats are drawn from.
                if isinstance(usage_log, ImageInferenceCallData):
                    await ChatRequestService.add_chat_request(
                        api_key_id=api_key.id,
                        input_tokens=0,
                        output_tokens=0,
                        cached_tokens=0,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                        db=db,
                    )
                else:
                    await ChatRequestService.add_chat_request(
                        api_key_id=api_key.id,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                        model_name=usage_log.model_name,
                        db=db,
                    )
            elif key_type == ApiKeyType.liberclaw:
                if isinstance(usage_log, ImageInferenceCallData):
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name, image_count=usage_log.image_count
                    )
                    success = await ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=credits_used,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                        db=db,
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
                        db=db,
                    )
                if not success:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found"
                    )
            elif key_type == ApiKeyType.x402:
                if isinstance(usage_log, ImageInferenceCallData):
                    actual_cost = await aleph_service.calculate_price(
                        model_id=usage_log.model_name, image_count=usage_log.image_count
                    )
                    success = await ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=actual_cost,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                        db=db,
                    )
                else:
                    actual_cost = await aleph_service.calculate_price(
                        model_id=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                    )
                    success = await ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=actual_cost,
                        model_name=usage_log.model_name,
                        input_tokens=usage_log.input_tokens,
                        output_tokens=usage_log.output_tokens,
                        cached_tokens=usage_log.cached_tokens,
                        db=db,
                    )
                if not success:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found"
                    )
                # Commit the metered usage before settling: settlement is an external
                # HTTP call, which must not run with the metering transaction open.
                # masked_key is read before the commit so the attribute is guaranteed
                # loaded (AsyncSessionLocal sets expire_on_commit=False today, but
                # this must not depend on that sessionmaker detail). The settlement
                # itself runs after the session block releases its connection.
                masked_key = api_key.masked_key
                await db.commit()
                x402_settlement = (
                    masked_key,
                    usage_log.payment_payload,
                    usage_log.payment_requirements,
                    actual_cost,
                )

            else:
                if isinstance(usage_log, ImageInferenceCallData):
                    credits_used = await aleph_service.calculate_price(
                        model_id=usage_log.model_name, image_count=usage_log.image_count
                    )
                    logger.debug(f"Calculated {credits_used} credits for image model {usage_log.model_name}")

                    success = await ApiKeyService.register_inference_call(
                        key=usage_log.key,
                        credits_used=credits_used,
                        model_name=usage_log.model_name,
                        image_count=usage_log.image_count,
                        db=db,
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
                        db=db,
                    )

                if not success:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND, detail=f"API key {usage_log.key} not found"
                    )

            # Commit metering, chat history, and the overflow deduction as one
            # transaction: a failure rolls back all of it, so the reporting gateway's
            # retry registers once instead of duplicating the usage report. x402
            # already committed above, before its external settlement call.
            if key_type != ApiKeyType.x402:
                await db.commit()

        if x402_settlement is not None:
            masked_key, payment_payload, payment_requirements, actual_cost = x402_settlement
            if payment_payload and payment_requirements:
                settled = await x402_service.settle_payment(payment_payload, payment_requirements, actual_cost)
                if not settled:
                    # Correlate the failed settlement with the just-committed usage
                    # row for operators; the cost disambiguates among concurrent
                    # calls on the same key; settle_payment never raises.
                    logger.warning(
                        f"x402 settlement failed for {masked_key} (${actual_cost} actual cost) — "
                        "usage metered but not settled"
                    )
            else:
                # A partial report (only one of the two fields) is a gateway bug —
                # name exactly which fields are missing so it stays debuggable.
                missing_fields = ", ".join(
                    name
                    for name, value in (
                        ("payment_payload", payment_payload),
                        ("payment_requirements", payment_requirements),
                    )
                    if not value
                )
                logger.warning(
                    f"x402 usage report for {masked_key} is missing {missing_fields} — usage metered but never settled"
                )

        return await _response()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging API key usage: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/admin/list", dependencies=[Depends(verify_admin_token)])  # type: ignore
async def get_admin_all_api_keys() -> ApiKeyAdminListResponse:
    try:
        result = await ApiKeyService.get_admin_all_api_keys()
        return ApiKeyAdminListResponse(keys=result.valid, invalid_keys=result.invalid, tiers=result.tiers)
    except Exception as e:
        logger.error(f"Error getting all API keys: {e!s}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")
