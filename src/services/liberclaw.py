import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.sql import func as sql_func

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.liberclaw import LiberclawApiKeyResponse, LiberclawUserResponse
from src.liberclaw_tiers import LIBERCLAW_TIERS, get_tier_config
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.inference_call import InferenceCall
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.liberclaw_user import LiberclawUser
from src.services.api_key_pool import ApiKeyPoolService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Liberclaw net window usage: grant-paid overflow must not drain the rolling allowance.
LIBERCLAW_NET_CREDITS = InferenceCall.credits_used - sql_func.coalesce(InferenceCall.liberclaw_extra_credits_used, 0.0)


class LiberclawService:
    @staticmethod
    async def resolve_by_account_id(db, account_id: uuid.UUID) -> LiberclawUser | None:
        """Identity-bridge lookup by liberclaw_account_id. Stable across the email
        changes that used to mint duplicate rows — callers holding an account id
        must try this before falling back to (user_id, user_type)."""
        return (
            (await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id)))
            .scalars()
            .first()
        )

    @staticmethod
    async def _refresh_email_if_safe(db, lc_user: LiberclawUser, user_id: str, user_type: str) -> None:
        """Refresh ``lc_user.user_id`` (email) in place to match a LiberClaw-side change.

        Guarded against unique_liberclaw_user (user_id, user_type): if another row
        already holds the incoming email for this user_type, the assignment is
        skipped (logged, resolution result unchanged) instead of raising
        IntegrityError on flush — which would 500 this account's api-key route on
        every subsequent call.
        """
        if user_type != "email" or lc_user.user_id == user_id:
            return
        conflict_id = (
            (
                await db.execute(
                    select(LiberclawUser.id).where(
                        LiberclawUser.user_id == user_id,
                        LiberclawUser.user_type == user_type,
                        LiberclawUser.id != lc_user.id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if conflict_id is not None:
            logger.error(
                f"Skipping email refresh on liberclaw user {lc_user.id} (account "
                f"{lc_user.liberclaw_account_id}): email {user_id!r} is already held by row {conflict_id}"
            )
            return
        lc_user.user_id = user_id

    @staticmethod
    async def get_or_create_api_key(
        user_id: str, user_type: str, liberclaw_account_id: uuid.UUID | None = None
    ) -> LiberclawApiKeyResponse:
        """Get existing or create new API key for a Liberclaw user.

        Resolves by ``liberclaw_account_id`` first (falls back to (user_id, user_type)
        for legacy rows with no account id yet). ``liberclaw_account_id`` is stored
        when the row is created, and backfilled if an existing row still has none —
        but never overwritten once set, since it is the identity bridge invoices key
        off of. When resolution hits on account id and the stored email differs from
        the incoming one, the stored email is refreshed in place (same row).
        """
        async with AsyncSessionLocal() as db:
            lc_user = None
            if liberclaw_account_id is not None:
                lc_user = await LiberclawService.resolve_by_account_id(db, liberclaw_account_id)
            if lc_user is None:
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
                lc_user = LiberclawUser(
                    user_id=user_id, user_type=user_type, liberclaw_account_id=liberclaw_account_id
                )
                db.add(lc_user)
                await db.flush()
            else:
                if lc_user.liberclaw_account_id is None and liberclaw_account_id is not None:
                    lc_user.liberclaw_account_id = liberclaw_account_id
                # user_id is only email-shaped for user_type="email" — discord/telegram
                # ids don't get refreshed off of a LiberClaw email change.
                await LiberclawService._refresh_email_if_safe(db, lc_user, user_id, user_type)

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
                # Same value, never rotated: running VMs hold it verbatim.
                existing_key.is_active = True
                await db.commit()
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
    async def deactivate_api_key(user_id: str, user_type: str) -> bool:
        """Suspend a Liberclaw user's key. Returns True if a key was deactivated."""
        async with AsyncSessionLocal() as db:
            key = (
                (
                    await db.execute(
                        select(ApiKeyDB)
                        .join(LiberclawUser, LiberclawUser.id == ApiKeyDB.liberclaw_user_id)
                        .where(
                            LiberclawUser.user_id == user_id,
                            LiberclawUser.user_type == user_type,
                            ApiKeyDB.type == ApiKeyType.liberclaw,
                            ApiKeyDB.is_active,
                        )
                    )
                )
                .scalars()
                .first()
            )

            if key is None:
                return False

            key.is_active = False
            await db.commit()
            return True

    @staticmethod
    async def update_tier(
        user_id: str, user_type: str, tier: str, liberclaw_account_id: uuid.UUID | None = None
    ) -> None:
        """Update tier for a Liberclaw user. Raises ValueError if tier invalid or user not found.

        Resolves by ``liberclaw_account_id`` first when given (falls back to
        (user_id, user_type)), refreshing the stored email in place on an account-id
        hit — same as ``get_or_create_api_key``.
        """
        if tier not in LIBERCLAW_TIERS:
            raise ValueError(f"Invalid tier '{tier}'. Valid tiers: {list(LIBERCLAW_TIERS.keys())}")

        async with AsyncSessionLocal() as db:
            lc_user = None
            if liberclaw_account_id is not None:
                lc_user = await LiberclawService.resolve_by_account_id(db, liberclaw_account_id)
            if lc_user is None:
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

            if lc_user.liberclaw_account_id is None and liberclaw_account_id is not None:
                lc_user.liberclaw_account_id = liberclaw_account_id
            await LiberclawService._refresh_email_if_safe(db, lc_user, user_id, user_type)

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

            tier_config = get_tier_config(lc_user.tier)
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

            last_call_at = (
                await db.execute(
                    select(sql_func.max(InferenceCall.used_at))
                    .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
                    .where(ApiKeyDB.liberclaw_user_id == lc_user.id)
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
                last_call_at=last_call_at,
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

            return await LiberclawService._create_grant(db, lc_user.id, amount, external_reference)

    @staticmethod
    async def grant_extra_credits_by_account_id(
        db, account_id: uuid.UUID, amount: float, external_reference: str
    ) -> float:
        """Fixed-amount grant keyed by liberclaw_account_id, for callers that already
        hold the account id rather than (user_id, user_type). Idempotent on
        ``external_reference`` like ``grant_extra_credits``. Raises ValueError on an
        unknown account.

        Flush-only: the caller owns the transaction (this runs mid-webhook,
        inside the caller's own session/commit).
        """
        existing = (
            (
                await db.execute(
                    select(LiberclawCreditGrant).where(LiberclawCreditGrant.external_reference == external_reference)
                )
            )
            .scalars()
            .first()
        )
        if existing:
            return existing.amount

        lc_user = await LiberclawService.resolve_by_account_id(db, account_id)
        if not lc_user:
            logger.error(f"grant_extra_credits_by_account_id: unknown liberclaw account {account_id}")
            raise ValueError(f"Liberclaw account not found: {account_id}")

        return await LiberclawService._create_grant(db, lc_user.id, amount, external_reference, commit=False)

    @staticmethod
    async def _create_grant(
        db, lc_user_id: uuid.UUID, amount: float, external_reference: str, commit: bool = True
    ) -> float:
        """Insert a credit grant row, flushing it in either case. Callers must have
        already checked ``external_reference`` for an existing grant (idempotency
        is their concern, not this helper's). ``commit=False`` leaves the
        transaction open for the caller to commit/rollback."""
        db.add(
            LiberclawCreditGrant(
                liberclaw_user_id=lc_user_id,
                amount=amount,
                external_reference=external_reference,
            )
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        logger.info(f"Granted {amount} extra credits to liberclaw user {lc_user_id} ({external_reference})")
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
