from calendar import month_abbr
from datetime import datetime, timedelta, date

from fastapi import HTTPException, status
from sqlalchemy import func, cast, Date, Integer, select, distinct, case, literal, and_

from src.config import config
from src.interfaces.api_keys import ApiKeyType
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
    GlobalUsersStats,
    UsersWindow,
    GlobalSegmentMessagesStats,
    SegmentMessageUsage,
    GlobalCreditsConsumptionStats,
    CreditsConsumptionDay,
    GlobalSubscriptionsStats,
    TierSubscribers,
    GlobalSubscribersOverTimeStats,
    TierSubscribersDay,
    LatestSubscriber,
    GlobalLatestSubscribersStats,
    MrrByTier,
    MrrDay,
    GlobalSubscriptionsRevenueStats,
    ChurnWeek,
    GlobalSubscriptionsChurnStats,
)
from src.models.anon_chat_usage import AnonChatUsage
from src.models.api_key import ApiKey
from src.models.base import AsyncSessionLocal
from src.models.chat_request import ChatRequest
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.subscription_tiers import get_tier
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


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
                    monthly_usage[month_abbr[mo]] = float(row.credits or 0)
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
        rows: list[tuple[date, str]], start_date: date, end_date: date, window_days: int
    ) -> GlobalUsersStats:
        """Rolling distinct-user counts from (day, identity) activity rows.

        For each day in [start_date, end_date], counts identities active in the trailing
        ``window_days`` days (inclusive). ``rows`` may (and for window > 1 must) include
        activity from before start_date. Days with a 0 count are omitted (sparse, matching
        the plain-DAU output). total_unique_users only counts activity inside the range.
        """
        by_day: dict[date, set[str]] = {}
        for day, ident in rows:
            by_day.setdefault(day, set()).add(ident)

        overall_in_range: set[str] = set()
        for day, idents in by_day.items():
            if start_date <= day <= end_date:
                overall_in_range |= idents

        daily: list[DailyActiveUsers] = []
        current = start_date
        while current <= end_date:
            window_start = current - timedelta(days=window_days - 1)
            active: set[str] = set()
            for day, idents in by_day.items():
                if window_start <= day <= current:
                    active |= idents
            if active:
                daily.append(DailyActiveUsers(date=current.strftime("%Y-%m-%d"), active_users=len(active)))
            current += timedelta(days=1)

        return GlobalUsersStats(total_unique_users=len(overall_in_range), daily_active_users=daily)

    @staticmethod
    async def _get_inference_users_stats(
        key_type: ApiKeyType, start_date: date, end_date: date, window: UsersWindow = UsersWindow.day
    ) -> GlobalUsersStats:
        """Daily active users + range-wide unique users for an inference key type.

        Identity is the owning user: ``api_keys.user_id`` for api/cli, ``api_keys.liberclaw_user_id``
        for liberclaw. NULL identities (legacy keys) are excluded. x402 has no identity at all, so
        it returns empty rather than relying on its NULL ``user_id`` to coincidentally count nothing.
        """
        if key_type == ApiKeyType.x402:
            return GlobalUsersStats(total_unique_users=0, daily_active_users=[])

        async with AsyncSessionLocal() as db:
            fetch_start = start_date - timedelta(days=window.days - 1)
            start_datetime = datetime.combine(fetch_start, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())

            identity = ApiKey.liberclaw_user_id if key_type == ApiKeyType.liberclaw else ApiKey.user_id
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
        """Daily active users + range-wide unique users for chat (separate chat_requests table)."""
        try:
            async with AsyncSessionLocal() as db:
                fetch_start = start_date - timedelta(days=window.days - 1)
                start_datetime = datetime.combine(fetch_start, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                raw = (
                    await db.execute(
                        select(
                            cast(ChatRequest.created_at, Date).label("date"),
                            ApiKey.user_id.label("ident"),
                        )
                        .select_from(ChatRequest)
                        .join(ApiKey, ChatRequest.api_key_id == ApiKey.id)
                        .where(
                            ApiKey.user_id.isnot(None),
                            ChatRequest.created_at >= start_datetime,
                            ChatRequest.created_at <= end_datetime,
                        )
                        .distinct()
                    )
                ).all()
                rows = [(r.date, str(r.ident)) for r in raw]
                return StatsService._rolling_users_stats(rows, start_date, end_date, window.days)
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
        """
        try:
            async with AsyncSessionLocal() as db:
                fetch_start = start_date - timedelta(days=window.days - 1)
                start_datetime = datetime.combine(fetch_start, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                inference_rows = (
                    await db.execute(
                        select(
                            cast(InferenceCall.used_at, Date).label("date"),
                            ApiKey.type.label("type"),
                            ApiKey.user_id.label("user_id"),
                            ApiKey.liberclaw_user_id.label("liberclaw_user_id"),
                        )
                        .select_from(InferenceCall)
                        .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
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
                        )
                        .select_from(ChatRequest)
                        .join(ApiKey, ChatRequest.api_key_id == ApiKey.id)
                        .where(
                            ApiKey.user_id.isnot(None),
                            ChatRequest.created_at >= start_datetime,
                            ChatRequest.created_at <= end_datetime,
                        )
                        .distinct()
                    )
                ).all()

                rows: list[tuple[date, str]] = []
                for r in inference_rows:
                    if r.type == ApiKeyType.liberclaw:
                        if r.liberclaw_user_id:
                            rows.append((r.date, f"l:{r.liberclaw_user_id}"))
                    elif r.user_id:
                        rows.append((r.date, f"u:{r.user_id}"))
                for cr in chat_rows:
                    if cr.user_id:
                        rows.append((cr.date, f"u:{cr.user_id}"))
                return StatsService._rolling_users_stats(rows, start_date, end_date, window.days)
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
    async def get_global_credits_consumption(start_date: date, end_date: date) -> GlobalCreditsConsumptionStats:
        """Credits consumed per day across api/cli/chat keys, split into the tier-covered portion
        (entitlement window) and the prepaid overflow (credits_used - tier_credits_used)."""
        try:
            async with AsyncSessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                chargeable = (ApiKeyType.api, ApiKeyType.chat, ApiKeyType.cli)
                rows = (
                    await db.execute(
                        select(
                            cast(InferenceCall.used_at, Date).label("date"),
                            func.coalesce(func.sum(InferenceCall.tier_credits_used), 0.0).label("tier"),
                            func.coalesce(
                                func.sum(InferenceCall.credits_used - InferenceCall.tier_credits_used), 0.0
                            ).label("prepaid"),
                        )
                        .select_from(InferenceCall)
                        .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                        .where(
                            InferenceCall.used_at >= start_datetime,
                            InferenceCall.used_at <= end_datetime,
                            ApiKey.type.in_(chargeable),
                        )
                        .group_by(cast(InferenceCall.used_at, Date))
                        .order_by(cast(InferenceCall.used_at, Date))
                    )
                ).all()

                total_tier = 0.0
                total_prepaid = 0.0
                daily = []
                for r in rows:
                    tier = round(float(r.tier or 0.0), 6)
                    prepaid = round(float(r.prepaid or 0.0), 6)
                    total_tier += tier
                    total_prepaid += prepaid
                    daily.append(
                        CreditsConsumptionDay(date=r.date.strftime("%Y-%m-%d"), tier_credits=tier, prepaid_credits=prepaid)
                    )
                return GlobalCreditsConsumptionStats(
                    total_credits=round(total_tier + total_prepaid, 6),
                    total_tier_credits=round(total_tier, 6),
                    total_prepaid_credits=round(total_prepaid, 6),
                    daily=daily,
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
    async def get_latest_subscribers(limit: int = 20) -> GlobalLatestSubscribersStats:
        """Most recent plan subscriptions (all providers), newest first, with a display label per user."""
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(PlanSubscription, User)
                        .join(User, PlanSubscription.user_id == User.id)
                        .order_by(PlanSubscription.created_at.desc())
                        .limit(limit)
                    )
                ).all()

                subscribers = []
                for sub, user in rows:
                    label = user.email or user.display_name or user.address or str(user.id)
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
                return GlobalLatestSubscribersStats(subscribers=subscribers)
        except Exception as e:
            logger.error(f"Error retrieving latest subscribers: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    # Events that end a subscription's paying life. ``cancelled_for_upgrade`` ends the old row of
    # an upgrade pair (the replacement row has its own ``activated``), avoiding double-counting.
    _TERMINAL_EVENTS = ("cancelled", "expired", "finished", "cancelled_for_upgrade")

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
    def _mrr_daily(timelines: list[dict], start_date: date, end_date: date) -> list[MrrDay]:
        daily: list[MrrDay] = []
        day = start_date
        while day <= end_date:
            total = 0.0
            for t in timelines:
                tier = StatsService._tier_at(t, day)
                if tier:
                    total += get_tier(tier).price_cents / 100
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

                today = date.today()
                by_tier: dict[str, float] = {}
                for t in timelines:
                    tier = StatsService._tier_at(t, today)
                    if tier:
                        by_tier[tier] = by_tier.get(tier, 0.0) + get_tier(tier).price_cents / 100
                current_mrr = round(sum(by_tier.values()), 2)
                return GlobalSubscriptionsRevenueStats(
                    current_mrr=current_mrr,
                    mrr_by_tier=[MrrByTier(tier=k, mrr=round(v, 2)) for k, v in sorted(by_tier.items())],
                    daily=daily,
                )
        except Exception as e:
            logger.error(f"Error retrieving subscriptions revenue: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def _churn_from_timelines(
        timelines: list[dict], start_date: date, end_date: date
    ) -> GlobalSubscriptionsChurnStats:
        """Weekly new vs churned subs. Weeks start Monday and are clipped to the requested range.

        An activation is not "new" if the same user had another sub terminated with
        ``cancelled_for_upgrade`` in the same ISO week (upgrade replacement).
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
                and t["terminal_event"] in ("cancelled", "expired", "finished")
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
