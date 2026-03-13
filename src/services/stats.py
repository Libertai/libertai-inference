from calendar import month_abbr
from datetime import datetime, timedelta, date

from fastapi import HTTPException, status
from sqlalchemy import func, cast, Date

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
)
from src.models.api_key import ApiKey
from src.models.base import SessionLocal
from src.models.chat_request import ChatRequest
from src.models.inference_call import InferenceCall
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class StatsService:
    @staticmethod
    def get_dashboard_stats(user_address: str) -> DashboardStats:
        try:
            with SessionLocal() as db:
                now = datetime.now()

                api_key_ids = [k.id for k in db.query(ApiKey.id).filter(ApiKey.user_address == user_address).all()]
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
                    db.query(
                        func.extract("year", InferenceCall.used_at).label("yr"),
                        func.extract("month", InferenceCall.used_at).label("mo"),
                        func.sum(InferenceCall.credits_used).label("credits"),
                        func.count(InferenceCall.id).label("calls"),
                        func.sum(InferenceCall.input_tokens).label("input_tokens"),
                        func.sum(InferenceCall.output_tokens).label("output_tokens"),
                    )
                    .filter(
                        InferenceCall.api_key_id.in_(api_key_ids),
                        InferenceCall.used_at >= six_months_start,
                    )
                    .group_by("yr", "mo")
                    .all()
                )

                monthly_usage = {}
                current_calls = 0
                current_credits = 0.0
                current_input = 0
                current_output = 0

                for row in monthly_rows:
                    mo = int(row.mo)
                    yr = int(row.yr)
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
    def get_usage_stats(user_address: str, start_date: date, end_date: date) -> UsageStats:
        try:
            with SessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                api_keys = db.query(ApiKey.id, ApiKey.name).filter(ApiKey.user_address == user_address).all()
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
                    db.query(
                        cast(InferenceCall.used_at, Date).label("date"),
                        func.sum(InferenceCall.input_tokens).label("input_tokens"),
                        func.sum(InferenceCall.output_tokens).label("output_tokens"),
                    )
                    .filter(*base_filter)
                    .group_by(cast(InferenceCall.used_at, Date))
                    .all()
                )

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
                    db.query(
                        InferenceCall.model_name.label("name"),
                        func.count(InferenceCall.id).label("calls"),
                        func.sum(InferenceCall.input_tokens + InferenceCall.output_tokens).label("total_tokens"),
                        func.sum(InferenceCall.credits_used).label("cost"),
                    )
                    .filter(*base_filter)
                    .group_by(InferenceCall.model_name)
                    .all()
                )

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
                    db.query(
                        InferenceCall.api_key_id.label("key_id"),
                        func.count(InferenceCall.id).label("calls"),
                        func.sum(InferenceCall.input_tokens + InferenceCall.output_tokens).label("total_tokens"),
                        func.sum(InferenceCall.credits_used).label("cost"),
                    )
                    .filter(*base_filter)
                    .group_by(InferenceCall.api_key_id)
                    .all()
                )

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
    def _get_inference_credits_stats(key_type: ApiKeyType, start_date: date, end_date: date) -> GlobalCreditsStats:
        with SessionLocal() as db:
            start_datetime = datetime.combine(start_date, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())

            model_stats = (
                db.query(
                    cast(InferenceCall.used_at, Date).label("date"),
                    InferenceCall.model_name.label("model_name"),
                    func.sum(InferenceCall.credits_used).label("credits"),
                )
                .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                .filter(
                    ApiKey.type == key_type,
                    InferenceCall.used_at >= start_datetime,
                    InferenceCall.used_at <= end_datetime,
                )
                .group_by(cast(InferenceCall.used_at, Date), InferenceCall.model_name)
                .order_by(cast(InferenceCall.used_at, Date))
                .all()
            )

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
    def get_global_credits_stats(start_date: date, end_date: date) -> GlobalCreditsStats:
        try:
            return StatsService._get_inference_credits_stats(ApiKeyType.api, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving credits stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def _get_inference_api_stats(key_type: ApiKeyType, start_date: date, end_date: date) -> GlobalApiStats:
        with SessionLocal() as db:
            start_datetime = datetime.combine(start_date, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())

            model_stats = (
                db.query(
                    cast(InferenceCall.used_at, Date).label("date"),
                    InferenceCall.model_name.label("name"),
                    func.count(InferenceCall.id).label("count"),
                )
                .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                .filter(
                    ApiKey.type == key_type,
                    InferenceCall.used_at >= start_datetime,
                    InferenceCall.used_at <= end_datetime,
                )
                .group_by(cast(InferenceCall.used_at, Date), InferenceCall.model_name)
                .order_by(cast(InferenceCall.used_at, Date))
                .all()
            )

            total = 0
            api_usage = []
            for stat in model_stats:
                total += stat.count
                api_usage.append(
                    ModelApiUsage(
                        model_name=stat.name,
                        used_at=stat.date.strftime("%Y-%m-%d"),
                        call_count=stat.count,
                    )
                )

            return GlobalApiStats(total_calls=total, api_usage=api_usage)

    @staticmethod
    def get_global_api_stats(start_date: date, end_date: date) -> GlobalApiStats:
        try:
            return StatsService._get_inference_api_stats(ApiKeyType.api, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving api stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def _get_inference_tokens_stats(key_type: ApiKeyType, start_date: date, end_date: date) -> GlobalTokensStats:
        with SessionLocal() as db:
            start_datetime = datetime.combine(start_date, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())

            inference_stats = (
                db.query(
                    cast(InferenceCall.used_at, Date).label("date"),
                    InferenceCall.model_name.label("model_name"),
                    func.sum(InferenceCall.input_tokens).label("input_tokens"),
                    func.sum(InferenceCall.output_tokens).label("output_tokens"),
                )
                .join(ApiKey, InferenceCall.api_key_id == ApiKey.id)
                .filter(
                    ApiKey.type == key_type,
                    InferenceCall.used_at >= start_datetime,
                    InferenceCall.used_at <= end_datetime,
                )
                .group_by(cast(InferenceCall.used_at, Date), InferenceCall.model_name)
                .order_by(cast(InferenceCall.used_at, Date))
                .all()
            )

            total_input = 0
            total_output = 0
            calls = []
            for stat in inference_stats:
                inp = stat.input_tokens or 0
                out = stat.output_tokens or 0
                total_input += inp
                total_output += out
                calls.append(
                    Call(
                        date=stat.date.strftime("%Y-%m-%d"),
                        nb_input_tokens=inp,
                        nb_output_tokens=out,
                        model_name=stat.model_name,
                    )
                )

            return GlobalTokensStats(total_input_tokens=total_input, total_output_tokens=total_output, calls=calls)

    @staticmethod
    def get_global_tokens_stats(start_date: date, end_date: date) -> GlobalTokensStats:
        try:
            return StatsService._get_inference_tokens_stats(ApiKeyType.api, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving token stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def get_global_chat_calls_stats(start_date: date, end_date: date) -> GlobalChatCallsStats:
        try:
            with SessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                chat_stats = (
                    db.query(
                        cast(ChatRequest.created_at, Date).label("date"),
                        ChatRequest.model_name.label("name"),
                        func.count(ChatRequest.id).label("count"),
                    )
                    .filter(ChatRequest.created_at >= start_datetime, ChatRequest.created_at <= end_datetime)
                    .group_by(cast(ChatRequest.created_at, Date), ChatRequest.model_name)
                    .order_by(cast(ChatRequest.created_at, Date))
                    .all()
                )

                total = 0
                chat_usage = []
                for stat in chat_stats:
                    total += stat.count
                    chat_usage.append(
                        ChatCallUsage(
                            model_name=stat.name,
                            used_at=stat.date.strftime("%Y-%m-%d"),
                            call_count=stat.count,
                        )
                    )

                return GlobalChatCallsStats(total_calls=total, chat_usage=chat_usage)
        except Exception as e:
            logger.error(f"Error retrieving chat calls stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def get_global_chat_tokens_stats(start_date: date, end_date: date) -> GlobalChatTokensStats:
        try:
            with SessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                chat_stats = (
                    db.query(
                        cast(ChatRequest.created_at, Date).label("date"),
                        ChatRequest.model_name.label("model_name"),
                        func.sum(ChatRequest.input_tokens).label("input_tokens"),
                        func.sum(ChatRequest.output_tokens).label("output_tokens"),
                        func.sum(ChatRequest.cached_tokens).label("cached_tokens"),
                    )
                    .filter(ChatRequest.created_at >= start_datetime, ChatRequest.created_at <= end_datetime)
                    .group_by(cast(ChatRequest.created_at, Date), ChatRequest.model_name)
                    .order_by(cast(ChatRequest.created_at, Date))
                    .all()
                )

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
    def get_global_liberclaw_calls_stats(start_date: date, end_date: date) -> GlobalApiStats:
        try:
            return StatsService._get_inference_api_stats(ApiKeyType.liberclaw, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving liberclaw calls stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def get_global_liberclaw_tokens_stats(start_date: date, end_date: date) -> GlobalTokensStats:
        try:
            return StatsService._get_inference_tokens_stats(ApiKeyType.liberclaw, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving liberclaw token stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def get_global_liberclaw_credits_stats(start_date: date, end_date: date) -> GlobalCreditsStats:
        try:
            return StatsService._get_inference_credits_stats(ApiKeyType.liberclaw, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving liberclaw credits stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def get_global_x402_calls_stats(start_date: date, end_date: date) -> GlobalApiStats:
        try:
            return StatsService._get_inference_api_stats(ApiKeyType.x402, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving x402 calls stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def get_global_x402_tokens_stats(start_date: date, end_date: date) -> GlobalTokensStats:
        try:
            return StatsService._get_inference_tokens_stats(ApiKeyType.x402, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving x402 token stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def get_global_x402_credits_stats(start_date: date, end_date: date) -> GlobalCreditsStats:
        try:
            return StatsService._get_inference_credits_stats(ApiKeyType.x402, start_date, end_date)
        except Exception as e:
            logger.error(f"Error retrieving x402 credits stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    def get_global_summary_stats(start_date: date, end_date: date) -> GlobalSummaryStats:
        try:
            with SessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                inference = (
                    db.query(
                        func.count(InferenceCall.id).label("cnt"),
                        func.coalesce(func.sum(InferenceCall.input_tokens), 0).label("inp"),
                        func.coalesce(func.sum(InferenceCall.output_tokens), 0).label("out"),
                    )
                    .filter(InferenceCall.used_at >= start_datetime, InferenceCall.used_at <= end_datetime)
                    .first()
                )

                chat = (
                    db.query(
                        func.count(ChatRequest.id).label("cnt"),
                        func.coalesce(func.sum(ChatRequest.input_tokens), 0).label("inp"),
                        func.coalesce(func.sum(ChatRequest.output_tokens), 0).label("out"),
                    )
                    .filter(ChatRequest.created_at >= start_datetime, ChatRequest.created_at <= end_datetime)
                    .first()
                )

                return GlobalSummaryStats(
                    total_requests=(inference.cnt or 0) + (chat.cnt or 0),
                    total_input_tokens=(inference.inp or 0) + (chat.inp or 0),
                    total_output_tokens=(inference.out or 0) + (chat.out or 0),
                )
        except Exception as e:
            logger.error(f"Error retrieving global summary stats: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")
