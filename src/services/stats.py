import uuid
from datetime import datetime, timedelta, date, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, cast, Date, Integer, select, distinct, case, literal, and_

from src.config import config
from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.interfaces.stats import (
    DashboardStats,
    TokenStats,
    UsageStats,
    DailyTokens,
    UsageByEntity,
    GlobalCreditsStats,
    CreditsUsage,
    GlobalApiStats,
    ModelApiUsage,
    GlobalTokensStats,
    Call,
    GlobalChatCallsStats,
    ChatCallUsage,
    GlobalChatTokensStats,
    ChatTokenUsage,
    GlobalSummaryStats,
    DailyActiveUsers,
    DailyTierActiveUsers,
    GlobalUsersStats,
    UsersWindow,
    GlobalSegmentMessagesStats,
    SegmentMessageUsage,
    SegmentCallUsage,
    GlobalSegmentCallsStats,
    GlobalCreditsConsumptionStats,
    CreditsConsumptionDay,
    TierCreditsDay,
    GlobalSubscriptionsStats,
    GlobalUserBaseActivityStats,
    TierSubscribers,
    GlobalSubscribersOverTimeStats,
    TierSubscribersDay,
    GlobalTierEconomicsStats,
    TierEconomicsDay,
    TierPrice,
    LatestSubscriber,
    GlobalLatestSubscribersStats,
    SubscriptionStatusFilter,
    SubscriptionActivityType,
    SubscriptionActivityEvent,
    GlobalSubscriptionActivityStats,
    MrrByTier,
    MrrDay,
    TopupDay,
    GlobalSubscriptionsRevenueStats,
    TopupRow,
    GlobalTopupsStats,
    ChurnWeek,
    GlobalSubscriptionsChurnStats,
)
from src.models.anon_chat_usage import AnonChatUsage
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.credit_transaction import CreditTransaction
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.subscription_tiers import get_tier, PAID_TIERS
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


def _tier_price(tier: str) -> float:
    """Price for a tier; unknown/legacy tier strings are skipped (0) instead of 500ing the endpoint."""
    try:
        return get_tier(tier).price_cents / 100
    except ValueError:
        logger.warning(f"Unknown subscription tier in MRR computation: {tier}")
        return 0.0


def _user_label(user: User) -> str:
    """Display label for a subscriber: ``display_name (contact)`` when a name is set, else the
    bare contact. ``contact`` resolves email > wallet address > user id."""
    contact = user.email or user.address or str(user.id)
    return f"{user.display_name} ({contact})" if user.display_name else contact


class StatsService:
    @staticmethod
    async def get_dashboard_stats(user_address: str) -> DashboardStats:
        try:
            async with AsyncSessionLocal() as db:
                now = datetime.now()

                api_key_ids = (
                    (await db.execute(select(ApiKey.id).where(ApiKey.user_address == user_address))).scalars().all()
                )
                if not api_key_ids:
                    return DashboardStats(
                        address=user_address,
                        monthly_usage={},
                        current_month=TokenStats(
                            inference_calls=0, total_tokens=0, input_tokens=0, output_tokens=0, credits_used=0.0
                        ),
                    )

                # 6 months ago boundary
                six_months_ago = datetime(now.year, now.month, 1) - timedelta(days=150)
                six_months_start = datetime(six_months_ago.year, six_months_ago.month, 1)

                # Single query: monthly credits + current month full stats
                monthly_rows = (
                    await db.execute(
                        select(
                            cast(func.extract("year", InferenceCall.used_at), Integer).label("yr"),
                            cast(func.extract("month", InferenceCall.used_at), Integer).label("mo"),
                            func.sum(InferenceCall.credits_used).label("credits"),
                            func.count(InferenceCall.id).label("calls"),
                            func.sum(InferenceCall.input_tokens).label("input_tokens"),
                            func.sum(InferenceCall.output_tokens).label("output_tokens"),
                        )
                        .where(
                            InferenceCall.api_key_id.in_(api_key_ids),
                            InferenceCall.used_at >= six_months_start,
                        )
                        .group_by("yr", "mo")
                        .order_by("yr", "mo")
                    )
                ).all()

                monthly_usage = {}
                current_calls = 0
                current_credits = 0.0
                current_input = 0
                current_output = 0

                for row in monthly_rows:
                    mo = row.mo
                    yr = row.yr
                    monthly_usage[f"{yr}-{mo:02d}"] = float(row.credits or 0)
                    if mo == now.month and yr == now.year:
                        current_calls = row.calls or 0
                        current_credits = float(row.credits or 0)
                        current_input = row.input_tokens or 0
                        current_output = row.output_tokens or 0

                return DashboardStats(
                    address=user_address,
                    monthly_usage=monthly_usage,
                    current_month=TokenStats(
                        inference_calls=current_calls,
                        input_tokens=current_input,
                        output_tokens=current_output,
                        total_tokens=current_input + current_output,
                        credits_used=current_credits,
                    ),
                )

        except Exception as e:
            logger.error(f"Error retrieving dashboard stats for {user_address}: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error retrieving dashboard statistics: {str(e)}",
            )

    @staticmethod
    async def get_usage_stats(user_address: str, start_date: date, end_date: date) -> UsageStats:
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                api_keys = (
                    await db.execute(select(ApiKey.id, ApiKey.name).where(ApiKey.user_address == user_address))
                ).all()
                if not api_keys:
                    return UsageStats(
                        inference_calls=0,
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                        cost=0.0,
                        daily_usage={},
                        usage_by_model=[],
                        usage_by_api_key=[],
                    )

                api_key_ids = [k.id for k in api_keys]
                api_key_lookup = {str(k.id): k.name for k in api_keys}

                base_filter = [
                    InferenceCall.api_key_id.in_(api_key_ids),
                    InferenceCall.used_at >= start_datetime,
                    InferenceCall.used_at <= end_datetime,
                ]

                # Daily usage — also derive totals from this
                daily_stats = (
                    await db.execute(
                        select(
                            cast(InferenceCall.used_at, Date).label("date"),
                            func.sum(InferenceCall.input_tokens).label("input_tokens"),
                            func.sum(InferenceCall.output_tokens).label("output_tokens"),
                        )
                        .where(*base_filter)
                        .group_by(cast(InferenceCall.used_at, Date))
                    )
                ).all()

                daily_data = {}
                total_input = 0
                total_output = 0
                for row in daily_stats:
                    inp = row.input_tokens or 0
                    out = row.output_tokens or 0
                    total_input += inp
                    total_output += out
                    daily_data[row.date.strftime("%Y-%m-%d")] = {"input_tokens": inp, "output_tokens": out}

                daily_usage = {}
                current_date = start_date
                while current_date <= end_date:
                    day_str = current_date.strftime("%Y-%m-%d")
                    d = daily_data.get(day_str, {"input_tokens": 0, "output_tokens": 0})
                    daily_usage[day_str] = DailyTokens(
                        input_tokens=d["input_tokens"], output_tokens=d["output_tokens"]
                    )
                    current_date += timedelta(days=1)

                # By model — also derive total calls + cost
                model_stats = (
                    await db.execute(
                        select(
                            InferenceCall.model_name.label("name"),
                            func.count(InferenceCall.id).label("calls"),
                            func.sum(InferenceCall.input_tokens + InferenceCall.output_tokens).label("total_tokens"),
                            func.sum(InferenceCall.credits_used).label("cost"),
                        )
                        .where(*base_filter)
                        .group_by(InferenceCall.model_name)
                    )
                ).all()

                total_calls = 0
                total_cost = 0.0
                usage_by_model = []
                for m in model_stats:
                    total_calls += m.calls or 0
                    total_cost += float(m.cost or 0)
                    usage_by_model.append(
                        UsageByEntity(
                            name=m.name,
                            calls=m.calls or 0,
                            total_tokens=m.total_tokens or 0,
                            cost=float(m.cost or 0),
                        )
                    )

                # By API key
                api_key_stats = (
                    await db.execute(
                        select(
                            InferenceCall.api_key_id.label("key_id"),
                            func.count(InferenceCall.id).label("calls"),
                            func.sum(InferenceCall.input_tokens + InferenceCall.output_tokens).label("total_tokens"),
                            func.sum(InferenceCall.credits_used).label("cost"),
                        )
                        .where(*base_filter)
                        .group_by(InferenceCall.api_key_id)
                    )
                ).all()

                usage_by_api_key = [
                    UsageByEntity(
                        name=api_key_lookup.get(str(k.key_id), "Unknown"),
                        calls=k.calls or 0,
                        total_tokens=k.total_tokens or 0,
                        cost=float(k.cost or 0),
                    )
                    for k in api_key_stats
                ]

                return UsageStats(
                    inference_calls=total_calls,
                    input_tokens=total_input,
                    output_tokens=total_output,
                    total_tokens=total_input + total_output,
                    cost=total_cost,
                    daily_usage=daily_usage,
                    usage_by_model=usage_by_model,
                    usage_by_api_key=usage_by_api_key,
                )

        except Exception as e:
            logger.error(f"Error retrieving usage stats for {user_address}: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error retrieving usage statistics: {str(e)}",
            )

    @staticmethod
    async def _get_inference_credits_stats(
        key_type: ApiKeyType, start_date: date, end_date: date
    ) -> GlobalCreditsStats:
        async with AsyncSessionLocal() as db:
            start_datetime = datetime.combine(start_date, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())

            model_stats = (
                await db.execute(
                    select(
                        cast(InferenceCall.used_at, Date).label("date"),
                        InferenceCall.model_name.label("model_name"),
                        func.sum(InferenceCall.credits_used).label("credits"),
                    )
                    .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                    .where(
                        ApiKey.type == key_type,
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .group_by(cast(InferenceCall.used_at, Date), InferenceCall.model_name)
                    .order_by(cast(InferenceCall.used_at, Date))
                )
            ).all()

            total = 0.0
            credits_usage = []
            for stat in model_stats:
                c = float(stat.credits or 0)
                total += c
                credits_usage.append(
                    CreditsUsage(
                        credits_used=c,
                        used_at=stat.date.strftime("%Y-%m-%d"),
                        model_name=stat.model_name,
                    )
                )

            return GlobalCreditsStats(total_credits_used=total, credits_usage=credits_usage)

    @staticmethod
    async def _get_inference_api_stats(key_type: ApiKeyType, start_date: date, end_date: date) -> GlobalApiStats:
        async with AsyncSessionLocal() as db:
            start_datetime = datetime.combine(start_date, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())

            model_stats = (
                await db.execute(
                    select(
                        cast(InferenceCall.used_at, Date).label("date"),
                        InferenceCall.model_name.label("name"),
                        func.count(InferenceCall.id).label("count"),
                    )
                    .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                    .where(
                        ApiKey.type == key_type,
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .group_by(cast(InferenceCall.used_at, Date), InferenceCall.model_name)
                    .order_by(cast(InferenceCall.used_at, Date))
                )
            ).all()

            total = 0
            api_usage = []
            for stat in model_stats:
                count: int = stat[2]  # func.count result
                total += count
                api_usage.append(
                    ModelApiUsage(
                        model_name=stat[1],
                        used_at=stat[0].strftime("%Y-%m-%d"),
                        call_count=count,
                    )
                )

            return GlobalApiStats(total_calls=total, api_usage=api_usage)

    @staticmethod
    async def _get_inference_tokens_stats(key_type: ApiKeyType, start_date: date, end_date: date) -> GlobalTokensStats:
        async with AsyncSessionLocal() as db:
            start_datetime = datetime.combine(start_date, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())

            inference_stats = (
                await db.execute(
                    select(
                        cast(InferenceCall.used_at, Date).label("date"),
                        InferenceCall.model_name.label("model_name"),
                        func.sum(InferenceCall.input_tokens).label("input_tokens"),
                        func.sum(InferenceCall.output_tokens).label("output_tokens"),
                        func.sum(InferenceCall.cached_tokens).label("cached_tokens"),
                    )
                    .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                    .where(
                        ApiKey.type == key_type,
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .group_by(cast(InferenceCall.used_at, Date), InferenceCall.model_name)
                    .order_by(cast(InferenceCall.used_at, Date))
                )
            ).all()

            total_input = 0
            total_output = 0
            total_cached = 0
            calls = []
            for stat in inference_stats:
                inp = stat.input_tokens or 0
                out = stat.output_tokens or 0
                cached = stat.cached_tokens or 0
                total_input += inp
                total_output += out
                total_cached += cached
                calls.append(
                    Call(
                        date=stat.date.strftime("%Y-%m-%d"),
                        nb_input_tokens=inp,
                        nb_output_tokens=out,
                        nb_cached_tokens=cached,
                        model_name=stat.model_name,
                    )
                )

            return GlobalTokensStats(
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                total_cached_tokens=total_cached,
                calls=calls,
            )

    @staticmethod
    def _rolling_users_stats(
        rows: list[tuple[date, str]],
        start_date: date,
        end_date: date,
        window_days: int,
        segment_by_ident: dict[str, str] | None = None,
    ) -> GlobalUsersStats:
        """Rolling distinct-user counts from (day, identity) activity rows.

        For each day in [start_date, end_date], counts identities active in the trailing
        ``window_days`` days (inclusive). ``rows`` may (and for window > 1 must) include
        activity from before start_date. Days with a 0 count are omitted (sparse, matching
        the plain-DAU output). total_unique_users only counts activity inside the range.

        ``segment_by_ident`` maps identity -> subscription segment; when given, per-day
        per-segment counts are emitted too (identities absent from the map count as
        "free"). Segments partition identities, so per-segment counts sum to the
        combined count for every window.
        """
        by_day: dict[date, set[str]] = {}
        for day, ident in rows:
            by_day.setdefault(day, set()).add(ident)

        overall_in_range: set[str] = set()
        for day, idents in by_day.items():
            if start_date <= day <= end_date:
                overall_in_range |= idents

        daily: list[DailyActiveUsers] = []
        by_tier: list[DailyTierActiveUsers] = []
        current = start_date
        while current <= end_date:
            window_start = current - timedelta(days=window_days - 1)
            active: set[str] = set()
            for day, idents in by_day.items():
                if window_start <= day <= current:
                    active |= idents
            if active:
                day_str = current.strftime("%Y-%m-%d")
                daily.append(DailyActiveUsers(date=day_str, active_users=len(active)))
                if segment_by_ident is not None:
                    seg_counts: dict[str, int] = {}
                    for ident in active:
                        seg = segment_by_ident.get(ident, "free")
                        seg_counts[seg] = seg_counts.get(seg, 0) + 1
                    for seg in sorted(seg_counts):
                        by_tier.append(DailyTierActiveUsers(date=day_str, tier=seg, active_users=seg_counts[seg]))
            current += timedelta(days=1)

        return GlobalUsersStats(
            total_unique_users=len(overall_in_range),
            daily_active_users=daily,
            daily_active_users_by_tier=by_tier,
        )

    @staticmethod
    async def _get_inference_users_stats(
        key_type: ApiKeyType, start_date: date, end_date: date, window: UsersWindow = UsersWindow.day
    ) -> GlobalUsersStats:
        """Daily active users + range-wide unique users for an inference key type.

        Identity is the owning user: ``api_keys.user_id`` for api/cli, ``api_keys.liberclaw_user_id``
        for liberclaw. NULL identities (legacy keys) are excluded. x402 has no identity at all, so
        it returns empty rather than relying on its NULL ``user_id`` to coincidentally count nothing.
        For api/cli, each identity also carries the owner's current subscription segment (active
        tier, else "free"), powering ``daily_active_users_by_tier``; liberclaw identities can't hold
        subscriptions so their by-tier list stays empty.
        """
        if key_type == ApiKeyType.x402:
            return GlobalUsersStats(total_unique_users=0, daily_active_users=[])

        async with AsyncSessionLocal() as db:
            fetch_start = start_date - timedelta(days=window.days - 1)
            start_datetime = datetime.combine(fetch_start, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())

            identity = ApiKey.liberclaw_user_id if key_type == ApiKeyType.liberclaw else ApiKey.user_id
            if key_type in (ApiKeyType.api, ApiKeyType.cli):
                segment = case(
                    (PlanSubscription.tier.isnot(None), PlanSubscription.tier),
                    else_=literal("free"),
                ).label("segment")
                raw = (
                    await db.execute(
                        select(
                            cast(InferenceCall.used_at, Date).label("date"),
                            identity.label("ident"),
                            segment,
                        )
                        .select_from(InferenceCall)
                        .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                        # At most one active subscription per user (unique constraint), so this
                        # left join adds at most one paid-tier row per call.
                        .outerjoin(
                            PlanSubscription,
                            and_(
                                PlanSubscription.user_id == ApiKey.user_id,
                                PlanSubscription.status == "active",
                            ),
                        )
                        .where(
                            ApiKey.type == key_type,
                            identity.isnot(None),
                            InferenceCall.used_at >= start_datetime,
                            InferenceCall.used_at <= end_datetime,
                        )
                        .distinct()
                    )
                ).all()
                rows = [(r.date, str(r.ident)) for r in raw]
                segment_by_ident = {str(r.ident): r.segment for r in raw}
                return StatsService._rolling_users_stats(rows, start_date, end_date, window.days, segment_by_ident)

            raw = (
                await db.execute(
                    select(
                        cast(InferenceCall.used_at, Date).label("date"),
                        identity.label("ident"),
                    )
                    .select_from(InferenceCall)
                    .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                    .where(
                        ApiKey.type == key_type,
                        identity.isnot(None),
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .distinct()
                )
            ).all()
            rows = [(r.date, str(r.ident)) for r in raw]
            return StatsService._rolling_users_stats(rows, start_date, end_date, window.days)

    @staticmethod
    async def get_global_chat_users_stats(
        start_date: date, end_date: date, window: UsersWindow = UsersWindow.day
    ) -> GlobalUsersStats:
        """Daily active users + range-wide unique users for chat (separate chat_requests table).

        Identities carry the owner's current subscription segment (active tier, else "free")
        for ``daily_active_users_by_tier``.
        """
        try:
            async with AsyncSessionLocal() as db:
                fetch_start = start_date - timedelta(days=window.days - 1)
                start_datetime = datetime.combine(fetch_start, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                segment = case(
                    (PlanSubscription.tier.isnot(None), PlanSubscription.tier),
                    else_=literal("free"),
                ).label("segment")

                raw = (
                    await db.execute(
                        select(
                            cast(ChatRequest.created_at, Date).label("date"),
                            ApiKey.user_id.label("ident"),
                            segment,
                        )
                        .select_from(ChatRequest)
                        .join(ApiKey, ChatRequest.api_key_id == ApiKey.id)
                        .outerjoin(
                            PlanSubscription,
                            and_(
                                PlanSubscription.user_id == ApiKey.user_id,
                                PlanSubscription.status == "active",
                            ),
                        )
                        .where(
                            ApiKey.user_id.isnot(None),
                            ChatRequest.created_at >= start_datetime,
                            ChatRequest.created_at <= end_datetime,
                        )
                        .distinct()
                    )
                ).all()
                rows = [(r.date, str(r.ident)) for r in raw]
                segment_by_ident = {str(r.ident): r.segment for r in raw}
                return StatsService._rolling_users_stats(rows, start_date, end_date, window.days, segment_by_ident)
        except Exception as e:
            logger.error(f"Error retrieving chat users stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_users_stats(
        start_date: date, end_date: date, window: UsersWindow = UsersWindow.day
    ) -> GlobalUsersStats:
        """Aggregate distinct users across api / cli / chat / liberclaw, deduplicated.

        api/cli/chat share ``users.id`` (namespaced ``u:``); liberclaw lives in its own
        identity space (``liberclaw_users.id``, namespaced ``l:``). A user active across
        several of api/cli/chat is therefore counted once. x402 keys have no identity and
        are excluded. Aggregation runs in Python over distinct (day, identity) rows — fine
        at DAU scale; revisit if the active-user volume grows large.

        Each identity also gets a subscription segment (current active tier, else "free";
        liberclaw identities are their own "liberclaw" segment) powering daily_active_users_by_tier.
        """
        try:
            async with AsyncSessionLocal() as db:
                fetch_start = start_date - timedelta(days=window.days - 1)
                start_datetime = datetime.combine(fetch_start, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                segment = case(
                    (PlanSubscription.tier.isnot(None), PlanSubscription.tier),
                    else_=literal("free"),
                ).label("segment")

                inference_rows = (
                    await db.execute(
                        select(
                            cast(InferenceCall.used_at, Date).label("date"),
                            ApiKey.type.label("type"),
                            ApiKey.user_id.label("user_id"),
                            ApiKey.liberclaw_user_id.label("liberclaw_user_id"),
                            segment,
                        )
                        .select_from(InferenceCall)
                        .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                        # At most one active subscription per user (unique constraint), so this
                        # left join adds at most one paid-tier row per call.
                        .outerjoin(
                            PlanSubscription,
                            and_(
                                PlanSubscription.user_id == ApiKey.user_id,
                                PlanSubscription.status == "active",
                            ),
                        )
                        .where(
                            ApiKey.type.in_([ApiKeyType.api, ApiKeyType.cli, ApiKeyType.liberclaw]),
                            InferenceCall.used_at >= start_datetime,
                            InferenceCall.used_at <= end_datetime,
                        )
                        .distinct()
                    )
                ).all()

                chat_rows = (
                    await db.execute(
                        select(
                            cast(ChatRequest.created_at, Date).label("date"),
                            ApiKey.user_id.label("user_id"),
                            segment,
                        )
                        .select_from(ChatRequest)
                        .join(ApiKey, ChatRequest.api_key_id == ApiKey.id)
                        .outerjoin(
                            PlanSubscription,
                            and_(
                                PlanSubscription.user_id == ApiKey.user_id,
                                PlanSubscription.status == "active",
                            ),
                        )
                        .where(
                            ApiKey.user_id.isnot(None),
                            ChatRequest.created_at >= start_datetime,
                            ChatRequest.created_at <= end_datetime,
                        )
                        .distinct()
                    )
                ).all()

                rows: list[tuple[date, str]] = []
                # Liberclaw identities can't hold a subscription; they get their own segment
                # rather than polluting "free".
                segment_by_ident: dict[str, str] = {}
                for r in inference_rows:
                    if r.type == ApiKeyType.liberclaw:
                        if r.liberclaw_user_id:
                            ident = f"l:{r.liberclaw_user_id}"
                            rows.append((r.date, ident))
                            segment_by_ident[ident] = "liberclaw"
                    elif r.user_id:
                        ident = f"u:{r.user_id}"
                        rows.append((r.date, ident))
                        segment_by_ident[ident] = r.segment
                for cr in chat_rows:
                    if cr.user_id:
                        ident = f"u:{cr.user_id}"
                        rows.append((cr.date, ident))
                        segment_by_ident[ident] = cr.segment
                return StatsService._rolling_users_stats(rows, start_date, end_date, window.days, segment_by_ident)
        except Exception as e:
            logger.error(f"Error retrieving aggregate users stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_chat_calls_stats(start_date: date, end_date: date) -> GlobalChatCallsStats:
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                chat_stats = (
                    await db.execute(
                        select(
                            cast(ChatRequest.created_at, Date).label("date"),
                            ChatRequest.model_name.label("name"),
                            func.count(ChatRequest.id).label("count"),
                        )
                        .where(ChatRequest.created_at >= start_datetime, ChatRequest.created_at <= end_datetime)
                        .group_by(cast(ChatRequest.created_at, Date), ChatRequest.model_name)
                        .order_by(cast(ChatRequest.created_at, Date))
                    )
                ).all()

                total = 0
                chat_usage = []
                for stat in chat_stats:
                    count: int = stat[2]  # func.count result
                    total += count
                    chat_usage.append(
                        ChatCallUsage(
                            model_name=stat[1],
                            used_at=stat[0].strftime("%Y-%m-%d"),
                            call_count=count,
                        )
                    )

                return GlobalChatCallsStats(total_calls=total, chat_usage=chat_usage)
        except Exception as e:
            logger.error(f"Error retrieving chat calls stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_chat_tokens_stats(start_date: date, end_date: date) -> GlobalChatTokensStats:
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                chat_stats = (
                    await db.execute(
                        select(
                            cast(ChatRequest.created_at, Date).label("date"),
                            ChatRequest.model_name.label("model_name"),
                            func.sum(ChatRequest.input_tokens).label("input_tokens"),
                            func.sum(ChatRequest.output_tokens).label("output_tokens"),
                            func.sum(ChatRequest.cached_tokens).label("cached_tokens"),
                        )
                        .where(ChatRequest.created_at >= start_datetime, ChatRequest.created_at <= end_datetime)
                        .group_by(cast(ChatRequest.created_at, Date), ChatRequest.model_name)
                        .order_by(cast(ChatRequest.created_at, Date))
                    )
                ).all()

                total_input = 0
                total_output = 0
                total_cached = 0
                token_usage = []
                for stat in chat_stats:
                    inp = stat.input_tokens or 0
                    out = stat.output_tokens or 0
                    cached = stat.cached_tokens or 0
                    total_input += inp
                    total_output += out
                    total_cached += cached
                    token_usage.append(
                        ChatTokenUsage(
                            date=stat.date.strftime("%Y-%m-%d"),
                            nb_input_tokens=inp,
                            nb_output_tokens=out,
                            nb_cached_tokens=cached,
                            model_name=stat.model_name,
                        )
                    )

                return GlobalChatTokensStats(
                    total_input_tokens=total_input,
                    total_output_tokens=total_output,
                    total_cached_tokens=total_cached,
                    token_usage=token_usage,
                )
        except Exception as e:
            logger.error(f"Error retrieving chat token stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_summary_stats(start_date: date, end_date: date) -> GlobalSummaryStats:
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                inference = (
                    await db.execute(
                        select(
                            func.count(InferenceCall.id).label("cnt"),
                            func.coalesce(func.sum(InferenceCall.input_tokens), 0).label("inp"),
                            func.coalesce(func.sum(InferenceCall.output_tokens), 0).label("out"),
                        ).where(InferenceCall.used_at >= start_datetime, InferenceCall.used_at <= end_datetime)
                    )
                ).first()

                chat = (
                    await db.execute(
                        select(
                            func.count(ChatRequest.id).label("cnt"),
                            func.coalesce(func.sum(ChatRequest.input_tokens), 0).label("inp"),
                            func.coalesce(func.sum(ChatRequest.output_tokens), 0).label("out"),
                        ).where(ChatRequest.created_at >= start_datetime, ChatRequest.created_at <= end_datetime)
                    )
                ).first()

                i_cnt, i_inp, i_out = (inference[0], inference[1], inference[2]) if inference else (0, 0, 0)
                c_cnt, c_inp, c_out = (chat[0], chat[1], chat[2]) if chat else (0, 0, 0)

                return GlobalSummaryStats(
                    total_requests=i_cnt + c_cnt,
                    total_input_tokens=i_inp + c_inp,
                    total_output_tokens=i_out + c_out,
                )
        except Exception as e:
            logger.error(f"Error retrieving global summary stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_messages_by_segment(start_date: date, end_date: date) -> GlobalSegmentMessagesStats:
        """Chat messages per subscription segment over time, from chat_requests (full history).

        Segment per message: the shared anonymous key -> "anonymous"; a user with an active paid
        subscription -> that tier (go/plus/max); otherwise "free". Tier history per message isn't
        stored, so messages are attributed to the sender's CURRENT tier.
        """
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                segment = case(
                    (ApiKey.key == config.LIBERTAI_CHAT_API_KEY, literal("anonymous")),
                    (PlanSubscription.tier.isnot(None), PlanSubscription.tier),
                    else_=literal("free"),
                ).label("segment")

                rows = (
                    await db.execute(
                        select(
                            cast(ChatRequest.created_at, Date).label("date"),
                            segment,
                            func.count(ChatRequest.id).label("cnt"),
                        )
                        .select_from(ChatRequest)
                        .join(ApiKey, ChatRequest.api_key_id == ApiKey.id)
                        # At most one active subscription per user (unique constraint), so this
                        # left join adds at most one paid-tier row per message.
                        .outerjoin(
                            PlanSubscription,
                            and_(
                                PlanSubscription.user_id == ApiKey.user_id,
                                PlanSubscription.status == "active",
                            ),
                        )
                        .where(ChatRequest.created_at >= start_datetime, ChatRequest.created_at <= end_datetime)
                        .group_by(cast(ChatRequest.created_at, Date), segment)
                        .order_by(cast(ChatRequest.created_at, Date))
                    )
                ).all()

                total = 0
                messages = []
                for r in rows:
                    count = int(r.cnt or 0)
                    total += count
                    messages.append(
                        SegmentMessageUsage(date=r.date.strftime("%Y-%m-%d"), segment=r.segment, message_count=count)
                    )
                return GlobalSegmentMessagesStats(total_messages=total, messages=messages)
        except Exception as e:
            logger.error(f"Error retrieving messages-by-segment stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_calls_by_segment(
        key_type: ApiKeyType, start_date: date, end_date: date
    ) -> GlobalSegmentCallsStats:
        """Inference calls per subscription segment over time, from inference_calls.

        Segment: a caller with an active paid subscription -> that tier (go/plus/max); otherwise
        "free". No anonymous bucket (api/cli keys are per-user). Only api/cli are meaningfully
        segmentable — liberclaw uses liberclaw_user_id (not user_id) and x402 has no identity, so
        both would bucket everything as "free"; return empty for anything but api/cli.
        """
        if key_type not in (ApiKeyType.api, ApiKeyType.cli):
            return GlobalSegmentCallsStats(total_calls=0, calls=[])
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                segment = case(
                    (PlanSubscription.tier.isnot(None), PlanSubscription.tier),
                    else_=literal("free"),
                ).label("segment")

                rows = (
                    await db.execute(
                        select(
                            cast(InferenceCall.used_at, Date).label("date"),
                            segment,
                            func.count(InferenceCall.id).label("cnt"),
                        )
                        .select_from(InferenceCall)
                        .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                        # At most one active subscription per user (unique constraint), so this
                        # left join adds at most one paid-tier row per call.
                        .outerjoin(
                            PlanSubscription,
                            and_(
                                PlanSubscription.user_id == ApiKey.user_id,
                                PlanSubscription.status == "active",
                            ),
                        )
                        .where(
                            ApiKey.type == key_type,
                            InferenceCall.used_at >= start_datetime,
                            InferenceCall.used_at <= end_datetime,
                        )
                        .group_by(cast(InferenceCall.used_at, Date), segment)
                        .order_by(cast(InferenceCall.used_at, Date))
                    )
                ).all()

                total = 0
                calls = []
                for r in rows:
                    count = int(r.cnt or 0)
                    total += count
                    calls.append(
                        SegmentCallUsage(date=r.date.strftime("%Y-%m-%d"), segment=r.segment, call_count=count)
                    )
                return GlobalSegmentCallsStats(total_calls=total, calls=calls)
        except Exception as e:
            logger.error(f"Error retrieving calls-by-segment stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_credits_consumption(start_date: date, end_date: date) -> GlobalCreditsConsumptionStats:
        """Credits consumed per day across api/cli/chat keys, split into the tier-covered portion
        (entitlement window) and the prepaid overflow (credits_used - tier_credits_used).

        Totals are rolled up from ``_credits_by_user_day`` (already grouped per user-day) instead
        of a second per-day scan over inference_calls — same table, same join, same filters.
        """
        try:
            async with AsyncSessionLocal() as db:
                timelines = await StatsService._all_subscription_timelines(db)
                user_day = await StatsService._credits_by_user_day(db, start_date, end_date)

                daily_totals: dict[date, list[float]] = {}
                for d, _uid, credits, tier_credits in user_day:
                    acc = daily_totals.setdefault(d, [0.0, 0.0])
                    acc[0] += tier_credits
                    acc[1] += credits - tier_credits

                total_tier = 0.0
                total_prepaid = 0.0
                daily = []
                for d, (tier_sum, prepaid_sum) in sorted(daily_totals.items()):
                    tier = round(tier_sum, 6)
                    prepaid = round(prepaid_sum, 6)
                    total_tier += tier
                    total_prepaid += prepaid
                    daily.append(
                        CreditsConsumptionDay(date=d.strftime("%Y-%m-%d"), tier_credits=tier, prepaid_credits=prepaid)
                    )

                by_tier = StatsService._aggregate_credits_by_tier(
                    [(d, uid, credits) for d, uid, credits, _tier_credits in user_day],
                    timelines,
                    start_date,
                    end_date,
                )
                daily_by_tier = [
                    TierCreditsDay(date=d.strftime("%Y-%m-%d"), tier=tier, credits=round(credits, 6))
                    for (d, tier), credits in sorted(by_tier.items())
                ]

                return GlobalCreditsConsumptionStats(
                    total_credits=round(total_tier + total_prepaid, 6),
                    total_tier_credits=round(total_tier, 6),
                    total_prepaid_credits=round(total_prepaid, 6),
                    daily=daily,
                    daily_by_tier=daily_by_tier,
                )
        except Exception as e:
            logger.error(f"Error retrieving credits-consumption stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_subscriptions_stats() -> GlobalSubscriptionsStats:
        """Current snapshot of the user base by segment: paid subscribers per tier, registered
        free users (no active paid sub), and anonymous users (distinct logged-out chat IPs)."""
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(
                            PlanSubscription.tier.label("tier"),
                            func.count(distinct(PlanSubscription.user_id)).label("cnt"),
                        )
                        .where(PlanSubscription.status == "active")
                        .group_by(PlanSubscription.tier)
                        .order_by(PlanSubscription.tier)
                    )
                ).all()
                by_tier = [TierSubscribers(tier=r.tier, active_subscribers=int(r.cnt or 0)) for r in rows]
                total_paid = sum(t.active_subscribers for t in by_tier)

                total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
                # Registered users without an active paid subscription.
                free_users = max(0, int(total_users) - total_paid)

                # One row per IP that has ever used logged-out chat.
                anonymous_users = (await db.execute(select(func.count(AnonChatUsage.id)))).scalar() or 0

                return GlobalSubscriptionsStats(
                    subscribers_by_tier=by_tier,
                    total_paid_subscribers=total_paid,
                    free_users=free_users,
                    anonymous_users=int(anonymous_users),
                )
        except Exception as e:
            logger.error(f"Error retrieving subscriptions stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_user_base_activity(start_date: date, end_date: date) -> GlobalUserBaseActivityStats:
        """Anonymous + free activity within a range, for the subscriptions user-base cards.

        Anonymous: AnonChatUsage rows whose current window started in the range — approximates
        IPs active in the range (only each IP's latest window is stored). Free: distinct account
        users active on api/cli/chat in the range with no active paid subscription (current
        attribution, matching the segment charts).
        """
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                anonymous = (
                    await db.execute(
                        select(func.count(AnonChatUsage.id)).where(
                            AnonChatUsage.window_started_at >= start_datetime,
                            AnonChatUsage.window_started_at <= end_datetime,
                        )
                    )
                ).scalar() or 0

                # No active subscription row -> free. The active-subscription left join matches at
                # most one row per user (partial unique constraint).
                no_active_sub = and_(
                    PlanSubscription.user_id == ApiKey.user_id,
                    PlanSubscription.status == "active",
                )

                free_idents: set[str] = set()
                inference_free = (
                    await db.execute(
                        select(ApiKey.user_id)
                        .select_from(InferenceCall)
                        .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                        .outerjoin(PlanSubscription, no_active_sub)
                        .where(
                            ApiKey.type.in_([ApiKeyType.api, ApiKeyType.cli]),
                            ApiKey.user_id.isnot(None),
                            PlanSubscription.id.is_(None),
                            InferenceCall.used_at >= start_datetime,
                            InferenceCall.used_at <= end_datetime,
                        )
                        .distinct()
                    )
                ).all()
                chat_free = (
                    await db.execute(
                        select(ApiKey.user_id)
                        .select_from(ChatRequest)
                        .join(ApiKey, ChatRequest.api_key_id == ApiKey.id)
                        .outerjoin(PlanSubscription, no_active_sub)
                        .where(
                            ApiKey.user_id.isnot(None),
                            PlanSubscription.id.is_(None),
                            ChatRequest.created_at >= start_datetime,
                            ChatRequest.created_at <= end_datetime,
                        )
                        .distinct()
                    )
                ).all()
                for r in inference_free:
                    free_idents.add(str(r.user_id))
                for r in chat_free:
                    free_idents.add(str(r.user_id))

                return GlobalUserBaseActivityStats(
                    anonymous_active_users=int(anonymous),
                    free_active_users=len(free_idents),
                )
        except Exception as e:
            logger.error(f"Error retrieving user-base activity stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_subscribers_over_time(start_date: date, end_date: date) -> GlobalSubscribersOverTimeStats:
        """Distinct paid subscribers per tier for each day in the range.

        Each subscription is paid over a span: from ``current_period_start`` until it ends (today for
        active/overdue, ``updated_at`` for cancelled/expired). Rows that never reached a paid period
        (``current_period_start IS NULL`` — abandoned checkouts, parked ``upgrading``/``pending``
        rows) are excluded. Each day counts DISTINCT users per tier, so a user who re-subscribes the
        same day (a churned span + a fresh one) is counted once. Counts use the CURRENT tier.
        """
        try:
            async with AsyncSessionLocal() as db:
                # Statuses that mean the subscription was (or still is) a real paid one.
                counted_statuses = ("active", "overdue", "cancelled", "expired")
                live_statuses = ("active", "overdue")
                rows = (
                    await db.execute(
                        select(
                            PlanSubscription.user_id,
                            PlanSubscription.tier,
                            PlanSubscription.status,
                            cast(PlanSubscription.current_period_start, Date).label("start"),
                            cast(PlanSubscription.updated_at, Date).label("updated"),
                        ).where(
                            PlanSubscription.status.in_(counted_statuses),
                            PlanSubscription.current_period_start.isnot(None),
                        )
                    )
                ).all()

                tiers = sorted({r.tier for r in rows})
                daily: list[TierSubscribersDay] = []
                day = start_date
                while day <= end_date:
                    per_tier: dict[str, set] = {t: set() for t in tiers}
                    for r in rows:
                        # Live subs run through today; ended subs stop on their last update.
                        end = end_date if r.status in live_statuses else r.updated
                        if r.start <= day <= max(end, r.start):
                            per_tier[r.tier].add(r.user_id)
                    for t in tiers:
                        daily.append(
                            TierSubscribersDay(
                                date=day.strftime("%Y-%m-%d"), tier=t, active_subscribers=len(per_tier[t])
                            )
                        )
                    day += timedelta(days=1)
                return GlobalSubscribersOverTimeStats(daily=daily)
        except Exception as e:
            logger.error(f"Error retrieving subscribers-over-time stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_tier_economics(start_date: date, end_date: date) -> GlobalTierEconomicsStats:
        """Per-tier subscribers and plan-covered credits per day, plus the tier price list."""
        try:
            async with AsyncSessionLocal() as db:
                timelines = await StatsService._all_subscription_timelines(db)
                user_day = await StatsService._credits_by_user_day(db, start_date, end_date)

                credits_by_tier = StatsService._aggregate_credits_by_tier(
                    [(d, uid, tier_credits) for d, uid, _credits, tier_credits in user_day],
                    timelines,
                    start_date,
                    end_date,
                )
                subs_by_tier = StatsService._subscribers_by_tier_day(timelines, start_date, end_date)

                daily: list[TierEconomicsDay] = []
                day = start_date
                while day <= end_date:
                    for tier in sorted(PAID_TIERS):
                        subscribers = subs_by_tier.get((day, tier), 0)
                        credits = credits_by_tier.get((day, tier), 0.0)
                        if subscribers == 0 and credits == 0.0:
                            continue
                        daily.append(
                            TierEconomicsDay(
                                date=day.strftime("%Y-%m-%d"),
                                tier=tier,
                                active_subscribers=subscribers,
                                credits=round(credits, 6),
                            )
                        )
                    day += timedelta(days=1)

                return GlobalTierEconomicsStats(
                    daily=daily,
                    tier_prices=[
                        TierPrice(
                            tier=t, monthly_price=_tier_price(t), weekly_credits=get_tier(t).weekly_credits
                        )
                        for t in sorted(PAID_TIERS, key=_tier_price)
                    ],
                )
        except Exception as e:
            logger.error(f"Error retrieving tier economics: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_latest_subscribers(
        limit: int | None = 20, statuses: list[SubscriptionStatusFilter] | None = None
    ) -> GlobalLatestSubscribersStats:
        """Most recent plan subscriptions (all providers), newest first, with a display label per user.

        ``statuses=None``/empty excludes ``pending`` rows (mostly abandoned checkouts); an ``all``
        entry returns everything; otherwise filters to exactly the given statuses. ``limit=None``
        returns every match; ``total`` always counts every match, ignoring ``limit``.
        """
        try:
            async with AsyncSessionLocal() as db:
                if not statuses:
                    condition = PlanSubscription.status != "pending"
                elif SubscriptionStatusFilter.all in statuses:
                    condition = None
                else:
                    condition = PlanSubscription.status.in_([s.value for s in statuses])

                count_stmt = select(func.count()).select_from(PlanSubscription)
                if condition is not None:
                    count_stmt = count_stmt.where(condition)
                total = (await db.execute(count_stmt)).scalar_one()

                stmt = (
                    select(PlanSubscription, User)
                    .join(User, PlanSubscription.user_id == User.id)
                    .order_by(PlanSubscription.created_at.desc())
                )
                if condition is not None:
                    stmt = stmt.where(condition)
                if limit is not None:
                    stmt = stmt.limit(limit)
                rows = (await db.execute(stmt)).all()

                subscribers = []
                for sub, user in rows:
                    label = _user_label(user)
                    subscribers.append(
                        LatestSubscriber(
                            user_label=label,
                            tier=sub.tier,
                            status=sub.status,
                            provider=sub.provider,
                            is_trial=sub.is_trial,
                            cancel_at_period_end=sub.cancel_at_period_end,
                            created_at=sub.created_at.isoformat(),
                            current_period_end=sub.current_period_end.isoformat()
                            if sub.current_period_end
                            else None,
                        )
                    )
                return GlobalLatestSubscribersStats(subscribers=subscribers, total=total)
        except Exception as e:
            logger.error(f"Error retrieving latest subscribers: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    # Raw event_type -> human-facing activity type. Only completed, meaningful transitions are
    # shown; intents (created/initiated/*_requested), bookkeeping (cancelled_for_upgrade,
    # upgrade_remainder_credited), reverts, abandoned checkouts (expired_abandoned_checkout),
    # declined cards on a checkout the user then retries (checkout_declined) and the redundant
    # ``overdue`` (``payment_failed`` covers the same incident) are all dropped. ``payment_failed``
    # therefore means a *live* sub failed to bill, never a fumbled card at signup.
    # ``upgraded`` (logged on the new sub at completion, metadata from/to) supersedes that sub's
    # ``activated`` so an upgrade reads as a single "from -> to" row.
    _ACTIVITY_TYPE_MAP = {
        "activated": SubscriptionActivityType.subscribed,
        "upgraded": SubscriptionActivityType.upgraded,
        "downgraded": SubscriptionActivityType.downgraded,
        "cancelled": SubscriptionActivityType.cancelled,
        "expired": SubscriptionActivityType.churned,
        "finished": SubscriptionActivityType.churned,
        "payment_failed": SubscriptionActivityType.payment_failed,
    }
    _ACTIVITY_TRANSITION_TYPES = (SubscriptionActivityType.upgraded, SubscriptionActivityType.downgraded)
    # Safety bound on raw events scanned per request. Lifecycle events grow slowly (a handful per
    # subscriber), so scanning them all keeps offset pagination and ``total`` exact; the cap only
    # guards against pathological growth.
    _ACTIVITY_SCAN_CAP = 10_000

    @staticmethod
    async def get_subscription_activity(
        limit: int = 20, types: list[SubscriptionActivityType] | None = None, offset: int = 0
    ) -> GlobalSubscriptionActivityStats:
        """Recent subscription lifecycle events, newest first, mapped to human-facing types.

        An upgrade completion logs an ``upgraded`` event (metadata from/to) on the new sub; that
        sub's ``activated`` is suppressed so the pair reads as one row. ``types=None``/empty
        returns every mapped type. ``offset``/``limit`` paginate the mapped stream; ``total`` is
        the full mapped count for the requested types.
        """
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    select(PlanSubscriptionEvent, PlanSubscription, User)
                    .join(PlanSubscription, PlanSubscriptionEvent.subscription_id == PlanSubscription.id)
                    .join(User, PlanSubscription.user_id == User.id)
                    .order_by(PlanSubscriptionEvent.created_at.desc())
                    .limit(StatsService._ACTIVITY_SCAN_CAP)
                )
                rows = (await db.execute(stmt)).all()

                # Subs whose activation is really an upgrade completion -> hide their "Subscribed".
                upgraded_subs = {e.subscription_id for e, _s, _u in rows if e.event_type == "upgraded"}

                wanted = set(types) if types else None
                events: list[SubscriptionActivityEvent] = []
                for event, sub, user in rows:
                    activity_type = StatsService._ACTIVITY_TYPE_MAP.get(event.event_type)
                    if activity_type is None:
                        continue
                    if event.event_type == "activated" and event.subscription_id in upgraded_subs:
                        continue  # the paired "Upgraded" row already represents this activation
                    if wanted is not None and activity_type not in wanted:
                        continue
                    meta = event.metadata_json or {}
                    if activity_type in StatsService._ACTIVITY_TRANSITION_TYPES:
                        from_tier = meta.get("from")
                        tier = meta.get("to") or sub.tier
                    else:
                        from_tier = None
                        tier = sub.tier
                    label = _user_label(user)
                    events.append(
                        SubscriptionActivityEvent(
                            created_at=event.created_at.isoformat(),
                            type=activity_type,
                            user_label=label,
                            tier=tier,
                            from_tier=from_tier,
                            provider=sub.provider,
                        )
                    )
                return GlobalSubscriptionActivityStats(events=events[offset : offset + limit], total=len(events))
        except Exception as e:
            logger.error(f"Error retrieving subscription activity: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    # Events that end a subscription's paying life. ``cancelled_for_upgrade`` ends the old row of
    # an upgrade pair (the replacement row has its own ``activated``), avoiding double-counting.
    _TERMINAL_EVENTS = ("cancelled", "expired", "finished", "cancelled_for_upgrade")
    # Terminal events that count as churn (excludes cancelled_for_upgrade, an upgrade replacement).
    _CHURN_TERMINAL_EVENTS = tuple(e for e in _TERMINAL_EVENTS if e != "cancelled_for_upgrade")

    @staticmethod
    def _replay_subscription_timelines(subs, events_by_sub) -> list[dict]:
        """Rebuild each sub's activation window and tier timeline from its event log.

        - activated_on: day of the FIRST ``activated`` event (renewals repeat it), None if never.
        - ended_on: day of the first terminal event (exclusive — no MRR that day), None if live.
        - tier_changes: [(activation_day, initial_tier)] + one entry per ``downgraded`` event.
          Initial tier from the ``created`` event metadata, falling back to the row's current tier
          (covers rows predating event logging).
        """
        timelines: list[dict] = []
        for sub in subs:
            events = sorted(events_by_sub.get(sub.id, []), key=lambda e: e.created_at)
            activated_on: date | None = None
            ended_on: date | None = None
            terminal_event: str | None = None
            initial_tier: str | None = None
            downgrades: list[tuple[date, str]] = []
            for e in events:
                day = e.created_at.date()
                if e.event_type == "created" and e.metadata_json and e.metadata_json.get("tier"):
                    initial_tier = e.metadata_json["tier"]
                elif e.event_type == "activated" and activated_on is None:
                    activated_on = day
                elif e.event_type == "downgraded" and e.metadata_json and e.metadata_json.get("to"):
                    downgrades.append((day, e.metadata_json["to"]))
                elif e.event_type == "downgraded":
                    logger.warning(
                        f"downgraded event {e.id} (subscription {sub.id}) missing tier metadata; "
                        "MRR keeps prior tier"
                    )
                elif e.event_type in StatsService._TERMINAL_EVENTS and ended_on is None:
                    ended_on = day
                    terminal_event = e.event_type
            tier_changes: list[tuple[date, str]] = []
            if activated_on is not None:
                tier_changes = [(activated_on, initial_tier or sub.tier)] + downgrades
            timelines.append(
                {
                    "user_id": sub.user_id,
                    "activated_on": activated_on,
                    "ended_on": ended_on,
                    "terminal_event": terminal_event,
                    "tier_changes": tier_changes,
                }
            )
        return timelines

    @staticmethod
    def _tier_at(timeline: dict, day: date) -> str | None:
        """The sub's tier on ``day``, or None if not active that day."""
        if timeline["activated_on"] is None or day < timeline["activated_on"]:
            return None
        if timeline["ended_on"] is not None and day >= timeline["ended_on"]:
            return None
        tier = None
        for change_day, change_tier in timeline["tier_changes"]:
            if change_day <= day:
                tier = change_tier
        return tier

    @staticmethod
    def _tier_by_user_day(timelines: list[dict], day: date) -> dict[uuid.UUID, str]:
        """user_id -> paid tier on ``day``. Users with no active sub that day are absent."""
        by_user: dict[uuid.UUID, str] = {}
        for t in timelines:
            tier = StatsService._tier_at(t, day)
            if tier:
                by_user[t["user_id"]] = tier
        return by_user

    @staticmethod
    def _aggregate_credits_by_tier(
        user_day_credits: list[tuple[date, uuid.UUID | None, float]],
        timelines: list[dict],
        start_date: date,
        end_date: date,
    ) -> dict[tuple[date, str], float]:
        """(day, tier) -> credits, attributed to the tier the user held THAT day.

        Users with no paid subscription on a day, and ownerless keys (no subscription by
        definition), bucket into "free".
        """
        tier_by_day: dict[date, dict[uuid.UUID, str]] = {}
        day = start_date
        while day <= end_date:
            tier_by_day[day] = StatsService._tier_by_user_day(timelines, day)
            day += timedelta(days=1)

        totals: dict[tuple[date, str], float] = {}
        for used_on, user_id, credits in user_day_credits:
            if used_on not in tier_by_day:
                continue
            tier = tier_by_day[used_on].get(user_id, "free") if user_id is not None else "free"
            key = (used_on, tier)
            totals[key] = totals.get(key, 0.0) + float(credits)
        return totals

    @staticmethod
    def _subscribers_by_tier_day(
        timelines: list[dict], start_date: date, end_date: date
    ) -> dict[tuple[date, str], int]:
        """(day, tier) -> distinct paid subscribers. Tiers with nobody that day are absent."""
        counts: dict[tuple[date, str], int] = {}
        day = start_date
        while day <= end_date:
            per_tier: dict[str, set[uuid.UUID]] = {}
            for t in timelines:
                tier = StatsService._tier_at(t, day)
                if tier:
                    per_tier.setdefault(tier, set()).add(t["user_id"])
            for tier, users in per_tier.items():
                counts[(day, tier)] = len(users)
            day += timedelta(days=1)
        return counts

    @staticmethod
    async def _credits_by_user_day(
        db, start_date: date, end_date: date
    ) -> list[tuple[date, uuid.UUID | None, float, float]]:
        """(day, user_id, credits_used, tier_credits_used) for chargeable keys.

        ``user_id`` is None for ownerless keys — callers bucket those into "free" (they have no
        subscription by definition). Aggregated in SQL so the caller maps a few hundred
        user-days to tiers, not ~1M rows.
        """
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())
        chargeable = (ApiKeyType.api, ApiKeyType.chat, ApiKeyType.cli)

        rows = (
            await db.execute(
                select(
                    cast(InferenceCall.used_at, Date).label("date"),
                    ApiKey.user_id.label("user_id"),
                    func.coalesce(func.sum(InferenceCall.credits_used), 0.0).label("credits"),
                    func.coalesce(func.sum(InferenceCall.tier_credits_used), 0.0).label("tier_credits"),
                )
                .select_from(InferenceCall)
                .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                .where(
                    InferenceCall.used_at >= start_datetime,
                    InferenceCall.used_at <= end_datetime,
                    ApiKey.type.in_(chargeable),
                )
                .group_by(cast(InferenceCall.used_at, Date), ApiKey.user_id)
            )
        ).all()
        return [(r.date, r.user_id, float(r.credits or 0.0), float(r.tier_credits or 0.0)) for r in rows]

    @staticmethod
    async def _all_subscription_timelines(db) -> list[dict]:
        """Replayed timelines for every subscription, ALL providers (trials excluded).

        The MRR path is Revolut-only because it measures cash; tier attribution is not — a
        credits-rail subscriber still holds a tier and burns credits against it.
        """
        subs = (
            (await db.execute(select(PlanSubscription).where(PlanSubscription.is_trial.is_(False))))
            .scalars()
            .all()
        )
        sub_ids = [s.id for s in subs]
        events = (
            (
                await db.execute(
                    select(PlanSubscriptionEvent).where(PlanSubscriptionEvent.subscription_id.in_(sub_ids))
                )
            )
            .scalars()
            .all()
            if sub_ids
            else []
        )
        events_by_sub: dict = {}
        for e in events:
            events_by_sub.setdefault(e.subscription_id, []).append(e)
        return StatsService._replay_subscription_timelines(subs, events_by_sub)

    @staticmethod
    async def _revolut_topups_by_day(db, start_date: date, end_date: date) -> list[tuple[date, float]]:
        """Completed Revolut credit purchases per day.

        ``upgrade_remainder:*`` rows are excluded: they are leftover subscription value refunded
        as credits on a tier upgrade, never a card payment. ``pending`` rows are excluded too —
        abandoned or stuck checkouts, money that was never collected.
        """
        start_datetime = datetime.combine(start_date, datetime.min.time())
        end_datetime = datetime.combine(end_date, datetime.max.time())

        rows = (
            await db.execute(
                select(
                    cast(CreditTransaction.created_at, Date).label("date"),
                    func.coalesce(func.sum(CreditTransaction.amount), 0.0).label("amount"),
                )
                .where(
                    CreditTransaction.provider == CreditTransactionProvider.revolut,
                    CreditTransaction.status == CreditTransactionStatus.completed,
                    CreditTransaction.created_at >= start_datetime,
                    CreditTransaction.created_at <= end_datetime,
                    func.coalesce(CreditTransaction.external_reference, "").not_like("upgrade_remainder:%"),
                )
                .group_by(cast(CreditTransaction.created_at, Date))
                .order_by(cast(CreditTransaction.created_at, Date))
            )
        ).all()
        return [(r.date, round(float(r.amount or 0.0), 2)) for r in rows]

    @staticmethod
    def _topups_window_start(start_date: date) -> date:
        """First day of ``start_date``'s calendar month.

        A month-to-date topups line for a range starting 5 July must still include 1-4 July,
        so the topups query reaches back further than the requested range.
        """
        return start_date.replace(day=1)

    @staticmethod
    def _mrr_daily(timelines: list[dict], start_date: date, end_date: date) -> list[MrrDay]:
        daily: list[MrrDay] = []
        day = start_date
        while day <= end_date:
            total = 0.0
            for t in timelines:
                tier = StatsService._tier_at(t, day)
                if tier:
                    total += _tier_price(tier)
            daily.append(MrrDay(date=day.strftime("%Y-%m-%d"), mrr=round(total, 2)))
            day += timedelta(days=1)
        return daily

    @staticmethod
    async def get_global_subscriptions_revenue(start_date: date, end_date: date) -> GlobalSubscriptionsRevenueStats:
        """Revolut MRR (nominal, currency-blind, trials excluded), event-replayed over the range."""
        try:
            async with AsyncSessionLocal() as db:
                subs = (
                    (
                        await db.execute(
                            select(PlanSubscription).where(
                                PlanSubscription.provider == "revolut",
                                PlanSubscription.is_trial.is_(False),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                sub_ids = [s.id for s in subs]
                events = (
                    (
                        await db.execute(
                            select(PlanSubscriptionEvent).where(
                                PlanSubscriptionEvent.subscription_id.in_(sub_ids)
                            )
                        )
                    ).scalars().all()
                    if sub_ids
                    else []
                )
                events_by_sub: dict = {}
                for e in events:
                    events_by_sub.setdefault(e.subscription_id, []).append(e)

                timelines = StatsService._replay_subscription_timelines(subs, events_by_sub)
                daily = StatsService._mrr_daily(timelines, start_date, end_date)

                topup_rows = await StatsService._revolut_topups_by_day(
                    db, StatsService._topups_window_start(start_date), end_date
                )
                topups_daily = [
                    TopupDay(date=d.strftime("%Y-%m-%d"), amount=amount) for d, amount in topup_rows
                ]
                total_topups = round(
                    sum(amount for d, amount in topup_rows if start_date <= d <= end_date), 2
                )

                today = datetime.now(timezone.utc).date()
                by_tier: dict[str, float] = {}
                for t in timelines:
                    tier = StatsService._tier_at(t, today)
                    if tier:
                        by_tier[tier] = by_tier.get(tier, 0.0) + _tier_price(tier)
                current_mrr = round(sum(by_tier.values()), 2)
                return GlobalSubscriptionsRevenueStats(
                    current_mrr=current_mrr,
                    mrr_by_tier=[MrrByTier(tier=k, mrr=round(v, 2)) for k, v in sorted(by_tier.items())],
                    daily=daily,
                    topups_daily=topups_daily,
                    total_topups=total_topups,
                )
        except Exception as e:
            logger.error(f"Error retrieving subscriptions revenue: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def get_global_revenue_topups(
        start_date: date, end_date: date, limit: int = 20, offset: int = 0
    ) -> GlobalTopupsStats:
        """Completed Revolut credit purchases in the range, newest first, paginated.

        Same filters as the topups revenue line: ``pending`` rows (money never collected) and
        ``upgrade_remainder:*`` rows (subscription value refunded as credits on upgrade, not a
        card payment) are excluded, so the table reconciles with the chart.
        """
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())
                conditions = (
                    CreditTransaction.provider == CreditTransactionProvider.revolut,
                    CreditTransaction.status == CreditTransactionStatus.completed,
                    CreditTransaction.created_at >= start_datetime,
                    CreditTransaction.created_at <= end_datetime,
                    func.coalesce(CreditTransaction.external_reference, "").not_like("upgrade_remainder:%"),
                )
                total = (await db.execute(select(func.count(CreditTransaction.id)).where(*conditions))).scalar() or 0
                rows = (
                    await db.execute(
                        select(CreditTransaction, User)
                        .join(User, CreditTransaction.user_id == User.id)
                        .where(*conditions)
                        .order_by(CreditTransaction.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
                # Buyer's subscription state: a live sub (active/overdue) wins over ended ones.
                subs_by_user: dict = {}
                user_ids = {tx.user_id for tx, _user in rows}
                if user_ids:
                    sub_rows = (
                        (
                            await db.execute(
                                select(PlanSubscription).where(
                                    PlanSubscription.user_id.in_(user_ids),
                                    PlanSubscription.status != "pending",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for sub in sub_rows:
                        if sub.status in ("active", "overdue"):
                            subs_by_user[sub.user_id] = sub.tier
                        else:
                            subs_by_user.setdefault(sub.user_id, "past")

                topups = [
                    TopupRow(
                        created_at=tx.created_at.isoformat(),
                        user_label=_user_label(user),
                        amount=round(float(tx.amount), 2),
                        used=round(float(tx.amount - tx.amount_left), 2),
                        subscription=subs_by_user.get(tx.user_id),
                    )
                    for tx, user in rows
                ]
                return GlobalTopupsStats(total=int(total), topups=topups)
        except Exception as e:
            logger.error(f"Error retrieving revenue topups: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def _churn_from_timelines(
        timelines: list[dict], start_date: date, end_date: date
    ) -> GlobalSubscriptionsChurnStats:
        """Weekly new vs churned subs. Weeks start Monday and are clipped to the requested range.

        An activation is not "new" if the same user had another sub terminated with
        ``cancelled_for_upgrade`` in the same ISO week (upgrade replacement). This match is by
        ISO week, so an upgrade pair straddling a week boundary (old sub ends Sunday, new
        activates Monday) counts as +1 new / 0 churned instead of being netted out — inherent
        to week bucketing, rare.
        """

        def week_of(day: date) -> date:
            return day - timedelta(days=day.weekday())

        upgrade_weeks: dict = {}
        for t in timelines:
            if t["terminal_event"] == "cancelled_for_upgrade" and t["ended_on"]:
                upgrade_weeks.setdefault(t["user_id"], set()).add(week_of(t["ended_on"]))

        weeks: dict[date, dict[str, int]] = {}
        w = week_of(start_date)
        while w <= end_date:
            weeks[w] = {"new": 0, "churned": 0}
            w += timedelta(days=7)

        total_new = 0
        total_churned = 0
        for t in timelines:
            a = t["activated_on"]
            if a and start_date <= a <= end_date:
                if week_of(a) not in upgrade_weeks.get(t["user_id"], set()):
                    weeks[week_of(a)]["new"] += 1
                    total_new += 1
            e = t["ended_on"]
            if (
                e
                and start_date <= e <= end_date
                and t["terminal_event"] in StatsService._CHURN_TERMINAL_EVENTS
            ):
                weeks[week_of(e)]["churned"] += 1
                total_churned += 1

        weekly = [
            ChurnWeek(week_start=w.strftime("%Y-%m-%d"), new=c["new"], churned=c["churned"], net=c["new"] - c["churned"])
            for w, c in sorted(weeks.items())
        ]
        return GlobalSubscriptionsChurnStats(weekly=weekly, total_new=total_new, total_churned=total_churned)

    @staticmethod
    async def get_global_subscriptions_churn(start_date: date, end_date: date) -> GlobalSubscriptionsChurnStats:
        """Revolut new-vs-churned subscribers per week (event-based; trials excluded)."""
        try:
            async with AsyncSessionLocal() as db:
                subs = (
                    (
                        await db.execute(
                            select(PlanSubscription).where(
                                PlanSubscription.provider == "revolut",
                                PlanSubscription.is_trial.is_(False),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                sub_ids = [s.id for s in subs]
                events = (
                    (
                        await db.execute(
                            select(PlanSubscriptionEvent).where(
                                PlanSubscriptionEvent.subscription_id.in_(sub_ids)
                            )
                        )
                    ).scalars().all()
                    if sub_ids
                    else []
                )
                events_by_sub: dict = {}
                for e in events:
                    events_by_sub.setdefault(e.subscription_id, []).append(e)
                timelines = StatsService._replay_subscription_timelines(subs, events_by_sub)
                return StatsService._churn_from_timelines(timelines, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving subscriptions churn: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")
