import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.sql import func as sql_func

from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.interfaces.api_keys import ApiKeyType
from src.interfaces.liberclaw import LiberclawApiKeyResponse, LiberclawUserResponse
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.liberclaw_user import LiberclawUser
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class LiberclawService:
    @staticmethod
    async def get_or_create_api_key(user_id: str, user_type: str) -> LiberclawApiKeyResponse:
        """Get existing or create new API key for a Liberclaw user."""
        async with AsyncSessionLocal() as db:
            lc_user = (
                (
                    await db.execute(
                        select(LiberclawUser).where(
                            LiberclawUser.user_id == user_id, LiberclawUser.user_type == user_type
                        )
                    )
                )
                .scalars()
                .first()
            )

            if not lc_user:
                lc_user = LiberclawUser(user_id=user_id, user_type=user_type)
                db.add(lc_user)
                await db.flush()

            existing_key = (
                (
                    await db.execute(
                        select(ApiKeyDB).where(
                            ApiKeyDB.liberclaw_user_id == lc_user.id,
                            ApiKeyDB.type == ApiKeyType.liberclaw,
                        )
                    )
                )
                .scalars()
                .first()
            )

            if existing_key:
                return LiberclawApiKeyResponse(key=existing_key.key, is_new=False)

            key = ApiKeyDB.generate_key()
            api_key = ApiKeyDB(
                key=key,
                name=f"liberclaw-{user_id}",
                type=ApiKeyType.liberclaw,
                liberclaw_user_id=lc_user.id,
            )
            db.add(api_key)
            await db.commit()

            return LiberclawApiKeyResponse(key=key, is_new=True)

    @staticmethod
    async def update_tier(user_id: str, user_type: str, tier: str) -> None:
        """Update tier for a Liberclaw user. Raises ValueError if tier invalid or user not found."""
        if tier not in LIBERCLAW_TIERS:
            raise ValueError(f"Invalid tier '{tier}'. Valid tiers: {list(LIBERCLAW_TIERS.keys())}")

        async with AsyncSessionLocal() as db:
            lc_user = (
                (
                    await db.execute(
                        select(LiberclawUser).where(
                            LiberclawUser.user_id == user_id, LiberclawUser.user_type == user_type
                        )
                    )
                )
                .scalars()
                .first()
            )

            if not lc_user:
                raise ValueError(f"Liberclaw user not found: {user_id} ({user_type})")

            lc_user.tier = tier
            await db.commit()

    @staticmethod
    async def get_user(user_id: str, user_type: str) -> LiberclawUserResponse:
        """Get Liberclaw user info with usage stats. Raises ValueError if not found."""
        async with AsyncSessionLocal() as db:
            lc_user = (
                (
                    await db.execute(
                        select(LiberclawUser).where(
                            LiberclawUser.user_id == user_id, LiberclawUser.user_type == user_type
                        )
                    )
                )
                .scalars()
                .first()
            )

            if not lc_user:
                raise ValueError(f"Liberclaw user not found: {user_id} ({user_type})")

            tier_config = LIBERCLAW_TIERS.get(lc_user.tier, LIBERCLAW_TIERS["free"])
            rolling_days = tier_config["rolling_window_days"]
            credits_limit = tier_config["credits_limit"]

            cutoff = datetime.now() - timedelta(days=rolling_days)
            usage = (
                await db.execute(
                    select(sql_func.coalesce(sql_func.sum(InferenceCall.credits_used), 0.0))
                    .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
                    .where(
                        ApiKeyDB.liberclaw_user_id == lc_user.id,
                        InferenceCall.used_at >= cutoff,
                    )
                )
            ).scalar()

            return LiberclawUserResponse(
                id=lc_user.id,
                user_id=lc_user.user_id,
                user_type=lc_user.user_type,
                tier=lc_user.tier,
                credits_used=float(usage or 0.0),
                credits_limit=credits_limit,
                rolling_window_days=rolling_days,
                created_at=lc_user.created_at,
            )

    @staticmethod
    async def get_rolling_window_usage(api_key_id: uuid.UUID, rolling_window_days: int) -> float:
        """Get total credits used by a key in the rolling window."""
        async with AsyncSessionLocal() as db:
            cutoff = datetime.now() - timedelta(days=rolling_window_days)
            result = (
                await db.execute(
                    select(sql_func.coalesce(sql_func.sum(InferenceCall.credits_used), 0.0)).where(
                        InferenceCall.api_key_id == api_key_id,
                        InferenceCall.used_at >= cutoff,
                    )
                )
            ).scalar()
            return float(result or 0.0)
