import uuid

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import func as sql_func

from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.interfaces.api_keys import ApiKey, FullApiKey, ApiKeyType
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.services.credit import CreditService
from src.services.entitlement import (
    WINDOW_5H,
    WINDOW_WEEKLY,
    active_tiers_by_users,
    compute_source,
    get_allowance_state,
    open_windows,
    window_usage_by_users,
)
from src.subscription_tiers import DEFAULT_TIER, get_tier
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class ApiKeyService:
    @staticmethod
    async def create_api_key(
        user_id: uuid.UUID,
        name: str,
        monthly_limit: float | None = None,
        key_type: ApiKeyType = ApiKeyType.api,
        user_address: str | None = None,
    ) -> FullApiKey:
        """
        Create a new API key for a user.

        Args:
            user_id: Owner user id
            name: Name for the API key
            monthly_limit: Optional monthly usage limit in credits
            key_type: Category/type of the API key
            user_address: Optional legacy wallet address to record on the key

        Returns:
            Newly created ApiKey object with all properties eagerly loaded
            This is the only time the FULL key is returned
        """
        logger.debug(f"Creating API key '{name}' for user {user_id}")

        try:
            async with AsyncSessionLocal() as db:
                # Check if name already exists for this user (ignoring soft-deleted keys,
                # so a deleted key's name can be reused).
                existing_key = (
                    (
                        await db.execute(
                            select(ApiKeyDB).where(
                                ApiKeyDB.user_id == user_id,
                                ApiKeyDB.name == name,
                                ApiKeyDB.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )

                if existing_key:
                    await db.rollback()
                    raise ValueError(f"API key with name '{name}' already exists")

                # Create new API key
                key = ApiKeyDB.generate_key()
                api_key = ApiKeyDB(
                    key=key,
                    name=name,
                    user_id=user_id,
                    user_address=user_address,
                    monthly_limit=monthly_limit,
                    type=key_type,
                )
                db.add(api_key)
                await db.commit()

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
                    type=api_key.type,
                )

        except Exception as e:
            logger.error(f"Error creating API key for user {user_id}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def get_or_create_chat_api_key(user_id: uuid.UUID, user_address: str | None = None) -> FullApiKey:
        """
        Get the chat API key for a user, or create one if it doesn't exist.

        Args:
            user_id: Owner user id
            user_address: Optional legacy wallet address to record on a newly created key

        Returns:
            FullApiKey object with the chat API key (full key returned only on creation)
        """
        logger.debug(f"Getting or creating chat API key for user {user_id}")

        try:
            async with AsyncSessionLocal() as db:
                # Check if chat API key already exists
                existing_key = (
                    (
                        await db.execute(
                            select(ApiKeyDB).where(ApiKeyDB.user_id == user_id, ApiKeyDB.type == ApiKeyType.chat)
                        )
                    )
                    .scalars()
                    .first()
                )

                if existing_key:
                    # Return existing key (masked)
                    return FullApiKey(
                        id=existing_key.id,
                        key=existing_key.masked_key,
                        full_key=existing_key.key,  # Return full key
                        name=existing_key.name,
                        user_address=existing_key.user_address,
                        created_at=existing_key.created_at,
                        is_active=existing_key.is_active,
                        monthly_limit=existing_key.monthly_limit,
                        type=existing_key.type,
                    )

                # Create new chat API key
                key = ApiKeyDB.generate_key()
                api_key = ApiKeyDB(
                    key=key,
                    name="Chat API Key",
                    user_id=user_id,
                    user_address=user_address,
                    monthly_limit=None,
                    type=ApiKeyType.chat,
                )
                db.add(api_key)
                await db.commit()

                return FullApiKey(
                    id=api_key.id,
                    key=api_key.masked_key,
                    full_key=key,
                    name=api_key.name,
                    user_address=api_key.user_address,
                    created_at=api_key.created_at,
                    is_active=api_key.is_active,
                    monthly_limit=api_key.monthly_limit,
                    type=api_key.type,
                )

        except Exception as e:
            logger.error(f"Error getting or creating chat API key for user {user_id}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def get_api_keys(user_id: uuid.UUID) -> list[FullApiKey]:
        """
        Get all API keys for a user with usage statistics.

        Args:
            user_id: Owner user id

        Returns:
            List of ApiKey objects with all properties eagerly loaded
            Keys are masked for security
        """
        logger.debug(f"Getting API keys for user {user_id}")

        try:
            async with AsyncSessionLocal() as db:
                # Get all API keys for the user
                api_keys = (
                    (
                        await db.execute(
                            select(ApiKeyDB).where(
                                ApiKeyDB.user_id == user_id,
                                ApiKeyDB.type == ApiKeyType.api,
                                ApiKeyDB.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

                # Create fully detached copies
                result = []
                for key in api_keys:
                    # Create a detached copy with all needed attributes
                    detached_key = FullApiKey(
                        key=key.masked_key,  # Masked key for display
                        full_key=key.key,
                        name=key.name,
                        user_address=key.user_address,
                        monthly_limit=key.monthly_limit,
                        id=key.id,
                        created_at=key.created_at,
                        is_active=key.is_active,
                        type=key.type,
                    )
                    result.append(detached_key)

                return result

        except Exception as e:
            logger.error(f"Error getting API keys for user {user_id}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def get_api_key_by_id(key_id: uuid.UUID) -> ApiKey | None:
        """
        Get a specific API key by ID.

        Args:
            key_id: API key UUID

        Returns:
            ApiKey object if found, None otherwise
            Key is masked for security
        """
        logger.debug(f"Getting API key with ID {key_id}")

        try:
            async with AsyncSessionLocal() as db:
                api_key = (await db.execute(select(ApiKeyDB).where(ApiKeyDB.id == key_id))).scalars().first()

                if not api_key:
                    return None

                return ApiKey(
                    key=api_key.masked_key,  # Masked key for display
                    name=api_key.name,
                    user_id=api_key.user_id,
                    user_address=api_key.user_address,
                    monthly_limit=api_key.monthly_limit,
                    id=api_key.id,
                    created_at=api_key.created_at,
                    is_active=api_key.is_active,
                    type=api_key.type,
                )

        except Exception as e:
            logger.error(f"Error getting API key with ID {key_id}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def update_api_key(
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

        try:
            async with AsyncSessionLocal() as db:
                api_key = (await db.execute(select(ApiKeyDB).where(ApiKeyDB.id == key_id))).scalars().first()

                if not api_key:
                    logger.warning(f"API key {key_id} not found for update")
                    return None

                # Update fields if provided
                if name is not None:
                    # Check if name already exists for this user
                    existing_key = (
                        (
                            await db.execute(
                                select(ApiKeyDB).where(
                                    ApiKeyDB.user_id == api_key.user_id,
                                    ApiKeyDB.name == name,
                                    ApiKeyDB.id != key_id,
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )

                    if existing_key:
                        logger.warning(f"API key with name '{name}' already exists for user {api_key.user_id}")
                        await db.rollback()
                        return None

                    api_key.name = name

                if is_active is not None:
                    api_key.is_active = is_active

                if monthly_limit is not None:
                    api_key.monthly_limit = monthly_limit

                await db.commit()

                return ApiKey(
                    key=api_key.masked_key,  # Masked key for display
                    name=api_key.name,
                    user_id=api_key.user_id,
                    user_address=api_key.user_address,
                    monthly_limit=api_key.monthly_limit,
                    id=api_key.id,
                    created_at=api_key.created_at,
                    is_active=api_key.is_active,
                    type=api_key.type,
                )

        except Exception as e:
            logger.error(f"Error updating API key {key_id}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def delete_api_key(key_id: uuid.UUID) -> bool:
        """
        Soft-delete an API key: mark it deleted + inactive instead of removing the row,
        so the related inference_calls (usage history) are preserved. A deleted key is
        hidden from the user and excluded from the inference gateway.

        Args:
            key_id: API key UUID

        Returns:
            Boolean indicating if the operation was successful
        """
        logger.debug(f"Soft-deleting API key {key_id}")

        try:
            async with AsyncSessionLocal() as db:
                api_key = (await db.execute(select(ApiKeyDB).where(ApiKeyDB.id == key_id))).scalars().first()

                if not api_key:
                    logger.warning(f"API key {key_id} not found for deletion")
                    return False

                if api_key.deleted_at is None:
                    api_key.deleted_at = datetime.now()
                    api_key.is_active = False
                    await db.commit()
                return True

        except Exception as e:
            logger.error(f"Error deleting API key {key_id}: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def get_admin_all_api_keys() -> list[str]:
        """
        Get all API keys across all addresses that have at least 0.02 credits available.
        This method is intended for admin use only.

        Returns:
            List of API key strings (unmasked) that meet the requirements
        """

        try:
            async with AsyncSessionLocal() as db:
                api_keys = (
                    (
                        await db.execute(
                            select(ApiKeyDB)
                            .where(ApiKeyDB.is_active, ApiKeyDB.deleted_at.is_(None))
                            .options(selectinload(ApiKeyDB.liberclaw_user))
                        )
                    )
                    .scalars()
                    .all()
                )

                # Pre-fetch balances for all users to avoid N+1 queries
                user_ids = {k.user_id for k in api_keys if k.user_id and k.type == ApiKeyType.api}
                balances: dict[uuid.UUID, float] = {}
                if user_ids:
                    from src.models.credit_transaction import CreditTransaction
                    from src.interfaces.credits import CreditTransactionStatus

                    balance_rows = (
                        await db.execute(
                            select(
                                CreditTransaction.user_id,
                                sql_func.coalesce(sql_func.sum(CreditTransaction.amount_left), 0.0),
                            )
                            .where(
                                CreditTransaction.user_id.in_(user_ids),
                                CreditTransaction.is_active == True,  # noqa: E712
                                CreditTransaction.status == CreditTransactionStatus.completed,
                            )
                            .group_by(CreditTransaction.user_id)
                        )
                    ).all()
                    balances = {row[0]: float(row[1]) for row in balance_rows}

                # Pre-fetch dual fixed-window entitlement inputs (usage within each user's
                # active 5h + weekly window, active tier) so the per-key loop is pure computation.
                now = datetime.now()
                window_5h_usage = await window_usage_by_users(db, user_ids, WINDOW_5H, now)
                weekly_usage = await window_usage_by_users(db, user_ids, WINDOW_WEEKLY, now)
                active_tiers = await active_tiers_by_users(db, user_ids)

                # Pre-fetch current month usage for API keys with monthly limits
                api_keys_with_limits = [
                    k for k in api_keys if k.type == ApiKeyType.api and k.monthly_limit is not None
                ]
                monthly_usage: dict[uuid.UUID, float] = {}
                if api_keys_with_limits:
                    now = datetime.now()
                    first_day = datetime(now.year, now.month, 1)
                    next_month = datetime(now.year + (now.month // 12), ((now.month % 12) + 1), 1)
                    limit_key_ids = [k.id for k in api_keys_with_limits]

                    usage_rows = (
                        await db.execute(
                            select(
                                InferenceCall.api_key_id,
                                sql_func.coalesce(sql_func.sum(InferenceCall.credits_used), 0.0),
                            )
                            .where(
                                InferenceCall.api_key_id.in_(limit_key_ids),
                                InferenceCall.used_at >= first_day,
                                InferenceCall.used_at < next_month,
                            )
                            .group_by(InferenceCall.api_key_id)
                        )
                    ).all()
                    monthly_usage = {row[0]: float(row[1]) for row in usage_rows}

                # Filter keys with sufficient credits available
                result = []
                for key in api_keys:
                    if key.type == ApiKeyType.liberclaw:
                        # Check rolling window usage against tier limit
                        if key.liberclaw_user_id is None:
                            continue
                        lc_user = key.liberclaw_user
                        if lc_user is None:
                            continue
                        tier_config = LIBERCLAW_TIERS.get(lc_user.tier, LIBERCLAW_TIERS["free"])
                        cutoff = datetime.now() - timedelta(days=tier_config["rolling_window_days"])
                        usage = float(
                            (
                                await db.execute(
                                    select(sql_func.coalesce(sql_func.sum(InferenceCall.credits_used), 0.0)).where(
                                        InferenceCall.api_key_id == key.id, InferenceCall.used_at >= cutoff
                                    )
                                )
                            ).scalar()
                            or 0.0
                        )
                        if usage >= tier_config["credits_limit"]:
                            continue
                    elif key.type == ApiKeyType.api:
                        if not key.user_id:
                            continue
                        # Per-key monthly limit is an extra cap (if the user set one).
                        if key.monthly_limit is not None:
                            key_usage = monthly_usage.get(key.id, 0.0)
                            if key_usage >= key.monthly_limit:
                                continue
                        # Dual-window entitlement: free tier (or larger paid windows) by
                        # default, prepaid balance as the overflow path.
                        tier = get_tier(active_tiers.get(key.user_id, DEFAULT_TIER))
                        source = compute_source(
                            tier,
                            window_5h_usage.get(key.user_id, 0.0),
                            weekly_usage.get(key.user_id, 0.0),
                            balances.get(key.user_id, 0.0),
                        )
                        if source == "blocked":
                            continue

                    # chat keys and valid liberclaw/api keys pass through
                    result.append(key.key)

                return result

        except Exception as e:
            logger.error(f"Error getting all API keys: {str(e)}", exc_info=True)
            raise

    @staticmethod
    async def register_inference_call(
        key: str,
        credits_used: float,
        model_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        image_count: int = 0,
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
            cached_tokens: Number of output tokens cached
            model_name: Name of the model used
            image_count: Number of images processed

        Returns:
            Boolean indicating if the operation was successful
        """
        logger.debug(f"Logging usage of {credits_used} credits for API key {key}")

        try:
            async with AsyncSessionLocal() as db:
                # Check if API key exists (even if inactive, we still want to log)
                api_key = (await db.execute(select(ApiKeyDB).where(ApiKeyDB.key == key))).scalars().first()

                if not api_key:
                    logger.warning(f"API key {key} not found")
                    return False

                # Anchor the usage row + windows to the same instant so this call counts
                # in the window it opens (avoids python/DB clock skew).
                now = datetime.now()
                usage = InferenceCall(
                    api_key_id=api_key.id,
                    credits_used=credits_used,
                    model_name=model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    image_count=image_count,
                )
                usage.used_at = now
                db.add(usage)

                # Chargeable keys (not liberclaw/x402) with an owner accrue against fixed windows.
                chargeable_user_id = (
                    api_key.user_id
                    if api_key.type not in (ApiKeyType.liberclaw, ApiKeyType.x402)
                    else None
                )
                if chargeable_user_id is not None:
                    # Open/reset this user's fixed windows so usage accrues against them.
                    await open_windows(db, chargeable_user_id, now)

                await db.commit()

                # Deduct credits from user's balance (skip for liberclaw and x402 keys).
                # Usage covered by a live tier window is NOT charged to prepaid — only
                # overflow beyond the fixed-window allowance draws down the balance.
                if chargeable_user_id is not None:
                    state = await get_allowance_state(db, chargeable_user_id, now)
                    if state.source != "tier":
                        success = await CreditService.use_credits(chargeable_user_id, credits_used)
                        if not success:
                            logger.warning(f"Failed to deduct {credits_used} credits for API key {key}")

                return True

        except Exception as e:
            logger.error(f"Error logging API key usage for {key}: {str(e)}", exc_info=True)
            raise
