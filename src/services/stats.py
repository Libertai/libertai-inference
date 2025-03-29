from calendar import month_abbr
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func

from src.interfaces.stats import DashboardStats, TokenStats
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
            db = SessionLocal()
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
                    total_tokens=(current_month_stats.input_tokens or 0) + (current_month_stats.output_tokens or 0),
                    credits_used=float(current_month_stats.credits or 0.0),
                ),
            )

            db.close()
            return result

        except Exception as e:
            logger.error(f"Error retrieving dashboard stats for {user_address}: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error retrieving dashboard statistics: {str(e)}",
            )
