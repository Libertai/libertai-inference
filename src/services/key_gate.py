"""One decision for "is this API key usable right now", shared by the whitelist the gateway
distributes and the answer a model server gets when it reports a call it already served.

The two callers differ only in how many keys they weigh at once, so the gate is split:
``static_key_status`` decides everything readable from the key row, ``fetch_key_aggregates``
bulk-loads the usage inputs for whatever it left undecided, and ``usage_key_status`` applies
the limits to those. Whitelist and usage report must reach the same verdict for a key.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import NamedTuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import func as sql_func

from src.config import config
from src.interfaces.api_keys import ApiKeyType, InvalidKeyReason
from src.interfaces.credits import CreditTransactionStatus
from src.liberclaw_tiers import get_tier_config
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.credit_transaction import CreditTransaction
from src.models.inference_call import InferenceCall
from src.models.liberclaw_credit_grant import LiberclawCreditGrant
from src.models.user import User
from src.services.entitlement import (
    CHARGEABLE_KEY_TYPES,
    PREPAID_MIN,
    WINDOW_5H,
    WINDOW_WEEKLY,
    active_tiers_by_users,
    compute_source,
    current_month_bounds,
    effective_prepaid,
    month_overflow_by_users,
    window_usage_by_users,
)
from src.subscription_tiers import DEFAULT_TIER, get_tier

# Every gateable key, with what the gate needs joined in. Keys hidden here are hidden from
# both callers: a deleted key and a suspended user's key are simply unknown, not explained.
GATEABLE_KEYS = (
    select(ApiKeyDB)
    # Outer join: liberclaw, x402 and pool keys have no user_id.
    .outerjoin(User, ApiKeyDB.user_id == User.id)
    .where(ApiKeyDB.deleted_at.is_(None))
    .where(or_(ApiKeyDB.user_id.is_(None), User.suspended_at.is_(None)))
    .options(selectinload(ApiKeyDB.liberclaw_user))
)


class KeyStatus(Enum):
    usable = auto()
    unusable = auto()  # carries a user-facing reason
    excluded = auto()  # unusable, with no reason to show (internal, ownership-broken, pruned)
    needs_usage = auto()  # decidable only against the usage aggregates


class KeyDecision(NamedTuple):
    status: KeyStatus
    reason: InvalidKeyReason | None = None


def _unusable(reason: InvalidKeyReason) -> KeyDecision:
    return KeyDecision(KeyStatus.unusable, reason)


@dataclass(frozen=True)
class KeyAggregates:
    """Usage inputs for one key's limit checks, all pre-fetched by ``fetch_key_aggregates``."""

    # Active tier name — the entitlement tier for chargeable keys, the liberclaw tier for
    # liberclaw keys. None for keys that have neither.
    tier_name: str | None = None
    monthly_usage: float = 0.0  # this key, this calendar month
    window_5h_usage: float = 0.0
    weekly_usage: float = 0.0
    prepaid: float = 0.0  # raw balance
    spendable_prepaid: float = 0.0  # what the monthly extra-credit cap still allows
    liberclaw_usage: float = 0.0  # net of grant-paid overflow
    liberclaw_limit: float = 0.0  # tier cap plus unconsumed grants


def static_key_status(key: ApiKeyDB, now: datetime, *, expired_keep_cutoff: datetime | None = None) -> KeyDecision:
    """Verdict from the key row alone, or ``needs_usage`` when the limits still have to run.

    ``expired_keep_cutoff`` prunes long-expired keys to ``excluded`` — the whitelist's
    invalid map is re-distributed continuously and must not grow unboundedly. Callers
    answering about a single key have no such map and pass None, keeping the reason.
    """
    if config.LIBERTAI_CHAT_API_KEY and key.key == config.LIBERTAI_CHAT_API_KEY:
        # Shared anonymous chat service key: always allowed, never gated.
        return KeyDecision(KeyStatus.usable)

    expired = key.expires_at is not None and key.expires_at < now

    if key.type == ApiKeyType.x402:
        # x402 requests carry their own payment auth; the key is internal and must never
        # surface user-facing reasons.
        return KeyDecision(KeyStatus.usable if key.is_active and not expired else KeyStatus.excluded)
    if key.type == ApiKeyType.pool:
        # Unclaimed pool keys are internal (no owner, never sent by users): usable ones ride
        # the whitelist so they're warm when claimed, dead ones get no user-facing reason.
        return KeyDecision(KeyStatus.usable if key.is_active and not expired else KeyStatus.excluded)

    # Ownership-broken keys are unusable but not user-explainable -> generic 401.
    if key.type == ApiKeyType.liberclaw and (key.liberclaw_user_id is None or key.liberclaw_user is None):
        return KeyDecision(KeyStatus.excluded)
    if key.type in CHARGEABLE_KEY_TYPES and not key.user_id:
        return KeyDecision(KeyStatus.excluded)

    if not key.is_active:
        return _unusable(InvalidKeyReason.disabled)
    if expired:
        assert key.expires_at is not None
        if expired_keep_cutoff is not None and key.expires_at < expired_keep_cutoff:
            return KeyDecision(KeyStatus.excluded)
        return _unusable(InvalidKeyReason.expired)
    return KeyDecision(KeyStatus.needs_usage)


def usage_key_status(key: ApiKeyDB, aggregates: KeyAggregates) -> KeyDecision:
    """Verdict for a key ``static_key_status`` left undecided, against its usage aggregates."""
    if key.type == ApiKeyType.liberclaw:
        if aggregates.liberclaw_usage >= aggregates.liberclaw_limit:
            return _unusable(InvalidKeyReason.liberclaw_limit)
        return KeyDecision(KeyStatus.usable)

    if key.type in CHARGEABLE_KEY_TYPES:
        # Per-key monthly limit is an extra cap (if the user set one).
        if key.monthly_limit is not None and aggregates.monthly_usage >= key.monthly_limit:
            return _unusable(InvalidKeyReason.key_monthly_limit)
        # Dual-window entitlement: free tier (or larger paid windows) by default, prepaid
        # balance as the overflow path.
        tier = get_tier(aggregates.tier_name or DEFAULT_TIER)
        source = compute_source(
            tier, aggregates.window_5h_usage, aggregates.weekly_usage, aggregates.spendable_prepaid
        )
        if source == "blocked":
            # Cap-blocked vs genuinely broke: raw prepaid distinguishes them.
            return _unusable(
                InvalidKeyReason.extra_credit_cap if aggregates.prepaid >= PREPAID_MIN else InvalidKeyReason.no_credits
            )

    return KeyDecision(KeyStatus.usable)


async def fetch_key_aggregates(
    db: AsyncSession, keys: list[ApiKeyDB], now: datetime
) -> dict[uuid.UUID, KeyAggregates]:
    """Usage inputs for ``keys``, keyed by key id. Batched per aggregate, not per key: the
    gateway weighs every key in the database on every refresh.
    """
    if not keys:
        return {}

    from src.services.liberclaw import LIBERCLAW_NET_CREDITS

    user_ids = {k.user_id for k in keys if k.user_id and k.type in CHARGEABLE_KEY_TYPES}

    balances: dict[uuid.UUID, float] = {}
    caps: dict[uuid.UUID, float] = {}
    cap_overflow: dict[uuid.UUID, float] = {}
    if user_ids:
        balance_rows = (
            await db.execute(
                select(
                    CreditTransaction.user_id,
                    sql_func.coalesce(sql_func.sum(CreditTransaction.amount_left), 0.0),
                )
                .where(
                    CreditTransaction.user_id.in_(user_ids),
                    CreditTransaction.is_active == True,
                    CreditTransaction.status == CreditTransactionStatus.completed,
                )
                .group_by(CreditTransaction.user_id)
            )
        ).all()
        balances = {row[0]: float(row[1]) for row in balance_rows}

        cap_rows = (
            await db.execute(
                select(User.id, User.monthly_extra_credit_cap).where(
                    User.id.in_(user_ids),
                    User.monthly_extra_credit_cap.is_not(None),
                )
            )
        ).all()
        caps = {row[0]: float(row[1]) for row in cap_rows}
        cap_overflow = await month_overflow_by_users(db, set(caps), now)

    # Dual fixed-window entitlement inputs (usage within each user's active 5h + weekly
    # window, active tier) so the per-key checks are pure computation.
    window_5h_usage = await window_usage_by_users(db, user_ids, WINDOW_5H, now)
    weekly_usage = await window_usage_by_users(db, user_ids, WINDOW_WEEKLY, now)
    active_tiers = await active_tiers_by_users(db, user_ids)

    # Current month usage for keys carrying a monthly limit.
    monthly_usage: dict[uuid.UUID, float] = {}
    limit_key_ids = [k.id for k in keys if k.type in CHARGEABLE_KEY_TYPES and k.monthly_limit is not None]
    if limit_key_ids:
        first_day, next_month = current_month_bounds(now)
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

    # Liberclaw rolling-window usage, grouped per distinct window so a shared window costs
    # one SUM rather than one per key.
    liberclaw_keys = [k for k in keys if k.type == ApiKeyType.liberclaw and k.liberclaw_user]
    liberclaw_usage: dict[uuid.UUID, float] = {}
    liberclaw_extra: dict[uuid.UUID, float] = {}
    if liberclaw_keys:
        key_ids_by_window: dict[int, list[uuid.UUID]] = {}
        for k in liberclaw_keys:
            lc_user = k.liberclaw_user
            if lc_user is None:
                continue
            key_ids_by_window.setdefault(get_tier_config(lc_user.tier)["rolling_window_days"], []).append(k.id)
        for window_days, key_ids in key_ids_by_window.items():
            cutoff = now - timedelta(days=window_days)
            rows = (
                await db.execute(
                    select(
                        InferenceCall.api_key_id,
                        sql_func.coalesce(sql_func.sum(LIBERCLAW_NET_CREDITS), 0.0),
                    )
                    .where(
                        InferenceCall.api_key_id.in_(key_ids),
                        InferenceCall.used_at >= cutoff,
                    )
                    .group_by(InferenceCall.api_key_id)
                )
            ).all()
            for row in rows:
                liberclaw_usage[row[0]] = float(row[1])

        # Unconsumed granted extra credits per liberclaw user (upgrade remainders) — they
        # extend the tier cap.
        extra_rows = (
            await db.execute(
                select(
                    LiberclawCreditGrant.liberclaw_user_id,
                    sql_func.coalesce(sql_func.sum(LiberclawCreditGrant.amount_left), 0.0),
                )
                .where(
                    LiberclawCreditGrant.liberclaw_user_id.in_({k.liberclaw_user_id for k in liberclaw_keys}),
                    LiberclawCreditGrant.amount_left > 0,
                )
                .group_by(LiberclawCreditGrant.liberclaw_user_id)
            )
        ).all()
        liberclaw_extra = {row[0]: float(row[1]) for row in extra_rows}

    aggregates: dict[uuid.UUID, KeyAggregates] = {}
    for key in keys:
        if key.type == ApiKeyType.liberclaw and key.liberclaw_user is not None:
            lc_user = key.liberclaw_user
            aggregates[key.id] = KeyAggregates(
                tier_name=lc_user.tier,
                liberclaw_usage=liberclaw_usage.get(key.id, 0.0),
                liberclaw_limit=get_tier_config(lc_user.tier)["credits_limit"] + liberclaw_extra.get(lc_user.id, 0.0),
            )
        elif key.type in CHARGEABLE_KEY_TYPES and key.user_id is not None:
            user_id = key.user_id
            prepaid = balances.get(user_id, 0.0)
            aggregates[key.id] = KeyAggregates(
                tier_name=get_tier(active_tiers.get(user_id, DEFAULT_TIER)).name,
                monthly_usage=monthly_usage.get(key.id, 0.0),
                window_5h_usage=window_5h_usage.get(user_id, 0.0),
                weekly_usage=weekly_usage.get(user_id, 0.0),
                prepaid=prepaid,
                spendable_prepaid=effective_prepaid(prepaid, caps.get(user_id), cap_overflow.get(user_id, 0.0)),
            )
        else:
            aggregates[key.id] = KeyAggregates()
    return aggregates
