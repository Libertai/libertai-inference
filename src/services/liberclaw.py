import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.sql import func as sql_func

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.liberclaw import LiberclawApiKeyResponse, LiberclawUserResponse
from src.liberclaw_tiers import LIBERCLAW_TIERS
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.liberclaw_user import LiberclawUser
from src.services.api_key_pool import ApiKeyPoolService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Liberclaw net window usage: grant-paid overflow must not drain the rolling allowance.
LIBERCLAW_NET_CREDITS = InferenceCall.credits_used - sql_func.coalesce(
    InferenceCall.liberclaw_extra_credits_used, 0.0
)


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

            claimed = await ApiKeyPoolService.claim_warm_key(
                db,
                target_type=ApiKeyType.liberclaw,
                name=f"liberclaw-{user_id}",
                liberclaw_user_id=lc_user.id,
            )
            if claimed is not None:
                api_key = claimed
            else:
                api_key = ApiKeyDB(
                    key=ApiKeyDB.generate_key(),
                    name=f"liberclaw-{user_id}",
                    type=ApiKeyType.liberclaw,
                    liberclaw_user_id=lc_user.id,
                )
                db.add(api_key)
            key = api_key.key
            await db.commit()

            if claimed is not None:
                ApiKeyPoolService.schedule_refill()

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
                    select(sql_func.coalesce(sql_func.sum(LIBERCLAW_NET_CREDITS), 0.0))
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
                extra_credits_left=await LiberclawService.extra_credits_left(db, lc_user.id),
                created_at=lc_user.created_at,
            )

    @staticmethod
    async def extra_credits_left(db, liberclaw_user_id: uuid.UUID) -> float:
        """Total unconsumed granted extra credits for a Liberclaw user."""
        total = (
            await db.execute(
                select(sql_func.coalesce(sql_func.sum(LiberclawCreditGrant.amount_left), 0.0)).where(
                    LiberclawCreditGrant.liberclaw_user_id == liberclaw_user_id
                )
            )
        ).scalar()
        return float(total or 0.0)

    @staticmethod
    async def grant_extra_credits(
        user_id: str, user_type: str, from_tier: str, unused_fraction: float, external_reference: str
    ) -> float:
        """Grant extra usage credits worth ``unused_fraction`` of ``from_tier``'s window cap.

        Used by Liberclaw to compensate the unused remainder of a plan cycle
        forfeited by a mid-cycle upgrade. Idempotent on ``external_reference``
        (webhook retries): an existing grant returns its original amount.
        Raises ValueError on unknown tier/user or fraction out of (0, 1].
        """
        if from_tier not in LIBERCLAW_TIERS:
            raise ValueError(f"Invalid tier '{from_tier}'. Valid tiers: {list(LIBERCLAW_TIERS.keys())}")
        if not 0.0 < unused_fraction <= 1.0:
            raise ValueError(f"unused_fraction must be in (0, 1], got {unused_fraction}")

        amount = round(LIBERCLAW_TIERS[from_tier]["credits_limit"] * unused_fraction, 2)
        if amount <= 0:
            raise ValueError("Grant amount rounds to zero")

        async with AsyncSessionLocal() as db:
            existing = (
                (
                    await db.execute(
                        select(LiberclawCreditGrant).where(
                            LiberclawCreditGrant.external_reference == external_reference
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                return existing.amount

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

            db.add(
                LiberclawCreditGrant(
                    liberclaw_user_id=lc_user.id,
                    amount=amount,
                    external_reference=external_reference,
                )
            )
            await db.commit()
            logger.info(
                f"Granted {amount} extra credits to liberclaw user {lc_user.id} ({external_reference})"
            )
            return amount

    @staticmethod
    async def lock_grants(db, liberclaw_user_id: uuid.UUID) -> list[LiberclawCreditGrant]:
        """FOR UPDATE lock on the user's unconsumed grants, oldest first.

        Callers that split a call between the window cap and grants must take this
        lock BEFORE reading window usage: it serializes concurrent overflow splits
        for the same user, so the second transaction re-reads the window only after
        the first has committed its usage row (otherwise both compute overflow from
        the same stale base and under-consume grants)."""
        return list(
            (
                await db.execute(
                    select(LiberclawCreditGrant)
                    .where(
                        LiberclawCreditGrant.liberclaw_user_id == liberclaw_user_id,
                        LiberclawCreditGrant.amount_left > 0,
                    )
                    .order_by(LiberclawCreditGrant.created_at)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    def decrement_grants(grants: list[LiberclawCreditGrant], amount: float) -> float:
        """Deduct up to ``amount`` from already-locked grants, oldest first. Returns
        what was actually consumed (partial when grants fall short — post-hoc
        billing, the call already happened)."""
        if amount <= 0:
            return 0.0
        remaining = amount
        for grant in grants:
            if remaining <= 0:
                break
            take = min(grant.amount_left, remaining)
            grant.amount_left = round(grant.amount_left - take, 10)
            remaining = round(remaining - take, 10)
        return round(amount - remaining, 10)

    @staticmethod
    async def consume_extra_credits(db, liberclaw_user_id: uuid.UUID, amount: float) -> float:
        """Lock + deduct in one step, within the caller's session/transaction."""
        if amount <= 0:
            return 0.0
        grants = await LiberclawService.lock_grants(db, liberclaw_user_id)
        return LiberclawService.decrement_grants(grants, amount)

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
