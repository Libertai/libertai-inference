from calendar import month_abbr
from datetime import datetime, timedelta, date
from typing import Any
from fastapi import HTTPException, status
from sqlalchemy import func, cast, Date

from src.interfaces.stats import DashboardStats, TokenStats, UsageStats, DailyTokens, UsageByEntity, CreditsStats, \
    CreditsConsumption
from src.models.api_key import ApiKey
from src.models.base import SessionLocal
from src.models.inference_call import InferenceCall
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class StatsService:
    @staticmethod
    def get_dashboard_stats(user_address: str) -> DashboardStats:
        """
        Get dashboard statistics for the user:
        - Credits used per month for the last 6 months
        - Number of inference calls made this month
        - Tokens used this month (input, output, and total)
        """
        try:
            with SessionLocal() as db:
                # Calculate monthly credit usage for the last 6 months
                now = datetime.now()
                monthly_usage = {}

                # Get all user's API keys
                api_keys = db.query(ApiKey).filter(ApiKey.user_address == user_address).all()
                if not api_keys:
                    return DashboardStats(
                        address=user_address,
                        monthly_usage={},
                        current_month=TokenStats(
                            inference_calls=0, total_tokens=0, input_tokens=0, output_tokens=0, credits_used=0.0
                        ),
                    )

                api_key_ids = [key.id for key in api_keys]

                # Get data for the last 6 months
                for i in range(5, -1, -1):
                    # Calculate month boundaries
                    month_date = now - timedelta(days=30 * i)
                    month_start = datetime(month_date.year, month_date.month, 1)

                    if month_date.month == 12:
                        next_month = datetime(month_date.year + 1, 1, 1)
                    else:
                        next_month = datetime(month_date.year, month_date.month + 1, 1)

                    # Month abbreviation (Jan, Feb, etc.)
                    month_key = month_abbr[month_date.month]

                    # Query credits used for this month
                    credits_used = (
                        db.query(func.sum(InferenceCall.credits_used))
                        .filter(
                            InferenceCall.api_key_id.in_(api_key_ids),
                            InferenceCall.used_at >= month_start,
                            InferenceCall.used_at < next_month,
                        )
                        .scalar()
                        or 0.0
                    )

                    monthly_usage[month_key] = float(credits_used)

                # Get current month statistics
                current_month_start = datetime(now.year, now.month, 1)
                current_month_stats: Any = (
                    db.query(
                        func.count(InferenceCall.id).label("calls"),
                        func.sum(InferenceCall.credits_used).label("credits"),
                        func.sum(InferenceCall.input_tokens).label("input_tokens"),
                        func.sum(InferenceCall.output_tokens).label("output_tokens"),
                    )
                    .filter(InferenceCall.api_key_id.in_(api_key_ids), InferenceCall.used_at >= current_month_start)
                    .first()
                )

                # Create the response
                result = DashboardStats(
                    address=user_address,
                    monthly_usage=monthly_usage,
                    current_month=TokenStats(
                        inference_calls=current_month_stats.calls or 0,
                        input_tokens=current_month_stats.input_tokens or 0,
                        output_tokens=current_month_stats.output_tokens or 0,
                        total_tokens=(current_month_stats.input_tokens or 0)
                        + (current_month_stats.output_tokens or 0),
                        credits_used=float(current_month_stats.credits or 0.0),
                    ),
                )

                return result

        except Exception as e:
            logger.error(f"Error retrieving dashboard stats for {user_address}: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error retrieving dashboard statistics: {str(e)}",
            )

    @staticmethod
    def get_usage_stats(user_address: str, start_date: date, end_date: date) -> UsageStats:
        """
        Get detailed usage statistics for a specific date range.

        Args:
            user_address: The address of the user
            start_date: Start date for the statistics period (inclusive)
            end_date: End date for the statistics period (inclusive)

        Returns:
            UsageStats object containing detailed usage statistics
        """
        try:
            with SessionLocal() as db:
                # Convert dates to datetime for database queries
                start_datetime = datetime.combine(start_date, datetime.min.time())
                # Add one day to end_date to make it inclusive
                end_datetime = datetime.combine(end_date, datetime.max.time())

                # Get all user's API keys
                api_keys = db.query(ApiKey).filter(ApiKey.user_address == user_address).all()
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

                api_key_ids = [key.id for key in api_keys]
                api_key_lookup = {str(key.id): key.name for key in api_keys}

                # Get overall statistics
                overall_stats: Any = (
                    db.query(
                        func.count(InferenceCall.id).label("calls"),
                        func.sum(InferenceCall.credits_used).label("credits"),
                        func.sum(InferenceCall.input_tokens).label("input_tokens"),
                        func.sum(InferenceCall.output_tokens).label("output_tokens"),
                    )
                    .filter(
                        InferenceCall.api_key_id.in_(api_key_ids),
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .first()
                )

                # Get daily usage statistics
                daily_stats = (
                    db.query(
                        cast(InferenceCall.used_at, Date).label("date"),
                        func.sum(InferenceCall.input_tokens).label("input_tokens"),
                        func.sum(InferenceCall.output_tokens).label("output_tokens"),
                    )
                    .filter(
                        InferenceCall.api_key_id.in_(api_key_ids),
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .group_by(cast(InferenceCall.used_at, Date))
                    .all()
                )

                # Convert database results to dictionary for easier lookup
                daily_data = {}
                for day_stat in daily_stats:
                    day_str = day_stat.date.strftime("%Y-%m-%d")
                    daily_data[day_str] = {
                        "input_tokens": day_stat.input_tokens or 0,
                        "output_tokens": day_stat.output_tokens or 0,
                    }

                # Generate all dates in the range and ensure they're all included
                daily_usage = {}
                current_date = start_date
                while current_date <= end_date:
                    day_str = current_date.strftime("%Y-%m-%d")
                    day_data = daily_data.get(day_str, {"input_tokens": 0, "output_tokens": 0})
                    daily_usage[day_str] = DailyTokens(
                        input_tokens=day_data["input_tokens"], output_tokens=day_data["output_tokens"]
                    )
                    current_date += timedelta(days=1)

                # Get usage by model
                model_stats = (
                    db.query(
                        InferenceCall.model_name.label("name"),
                        func.count(InferenceCall.id).label("calls"),
                        func.sum(InferenceCall.input_tokens + InferenceCall.output_tokens).label("total_tokens"),
                        func.sum(InferenceCall.credits_used).label("cost"),
                    )
                    .filter(
                        InferenceCall.api_key_id.in_(api_key_ids),
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .group_by(InferenceCall.model_name)
                    .all()
                )

                usage_by_model = [
                    UsageByEntity(
                        name=model.name,
                        calls=model.calls or 0,
                        total_tokens=model.total_tokens or 0,
                        cost=float(model.cost or 0.0),
                    )
                    for model in model_stats
                ]

                # Get usage by API key
                api_key_stats = (
                    db.query(
                        InferenceCall.api_key_id.label("key_id"),
                        func.count(InferenceCall.id).label("calls"),
                        func.sum(InferenceCall.input_tokens + InferenceCall.output_tokens).label("total_tokens"),
                        func.sum(InferenceCall.credits_used).label("cost"),
                    )
                    .filter(
                        InferenceCall.api_key_id.in_(api_key_ids),
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .group_by(InferenceCall.api_key_id)
                    .all()
                )

                usage_by_api_key = [
                    UsageByEntity(
                        name=api_key_lookup.get(str(key.key_id), "Unknown"),
                        calls=key.calls or 0,
                        total_tokens=key.total_tokens or 0,
                        cost=float(key.cost or 0.0),
                    )
                    for key in api_key_stats
                ]

                # Create the response
                result = UsageStats(
                    inference_calls=overall_stats.calls or 0,
                    input_tokens=overall_stats.input_tokens or 0,
                    output_tokens=overall_stats.output_tokens or 0,
                    total_tokens=(overall_stats.input_tokens or 0) + (overall_stats.output_tokens or 0),
                    cost=float(overall_stats.credits or 0.0),
                    daily_usage=daily_usage,
                    usage_by_model=usage_by_model,
                    usage_by_api_key=usage_by_api_key,
                )

                return result

        except Exception as e:
            logger.error(f"Error retrieving usage stats for {user_address}: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error retrieving usage statistics: {str(e)}",
            )

    @staticmethod
    def get_credits_stats(start_date: date, end_date: date) -> CreditsStats:
        """
        Get model usage for a specific date range.

        Args:
            start_date: Start date for the statistics period (inclusive)
            end_date: End date for the statistics period (inclusive)

        Returns:
            Name of the model used in the date range ("hermes" if used, otherwise "yolo")
        """
        try:
            with SessionLocal() as db:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                total_credits_used = db.query(func.sum(InferenceCall.credits_used)).scalar()

                model_stats = (
                    db.query(
                        InferenceCall.credits_used.label("credits"),
                        InferenceCall.used_at.label("used_at"),
                        InferenceCall.model_name.label("name"),
                    )
                    .filter(
                        InferenceCall.used_at >= start_datetime,
                        InferenceCall.used_at <= end_datetime,
                    )
                    .all()
                )

                credits_consumption = [
                    CreditsConsumption(
                        credits_used=ai_model.credits,
                        used_at=ai_model.used_at.strftime('%Y-%m-%d'),
                        model_name=ai_model.name,
                    )
                    for ai_model in model_stats
                ]

                return CreditsStats(
                    total_credits_used=total_credits_used,
                    credits_consumption=credits_consumption
                )
        except Exception as e:
            logger.error(f"Error retrieving credits stats: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Internal server error"
            )