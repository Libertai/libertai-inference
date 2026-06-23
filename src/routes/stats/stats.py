from datetime import date

from fastapi import Depends, Query

from src.interfaces.api_keys import ApiKeyType, InferenceKeyType
from src.interfaces.stats import (
    DashboardStats,
    UsageStats,
    GlobalCreditsStats,
    GlobalApiStats,
    GlobalTokensStats,
    GlobalChatCallsStats,
    GlobalChatTokensStats,
    GlobalSummaryStats,
    GlobalUsersStats,
    GlobalSegmentMessagesStats,
    GlobalCreditsConsumptionStats,
    GlobalSubscriptionsStats,
    GlobalSubscribersOverTimeStats,
)
from src.models.user import User
from src.routes.stats import router
from src.services.auth import get_current_user
from src.services.stats import StatsService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.get("/dashboard", response_model=DashboardStats)  # type: ignore
async def get_dashboard_stats(user: User = Depends(get_current_user)) -> DashboardStats:
    try:
        # Stats are still wallet-address keyed; email users (no address) get empty stats for now.
        return await StatsService.get_dashboard_stats(user.address or "")
    except Exception as e:
        logger.error(f"Error in dashboard stats route for user {user.id}: {str(e)}", exc_info=True)
        raise


@router.get("/usage", response_model=UsageStats)  # type: ignore
async def get_usage_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
    user: User = Depends(get_current_user),
) -> UsageStats:
    try:
        return await StatsService.get_usage_stats(user.address or "", start_date, end_date)
    except Exception as e:
        logger.error(f"Error in usage stats route for user {user.id}: {str(e)}", exc_info=True)
        raise


# --- Chat stats: separate table (chat_requests), no key-type filter, no credits ---
# Registered before the generic /global/{key_type}/... routes below.


@router.get("/global/chat/calls", response_model=GlobalChatCallsStats)  # type: ignore
async def get_chat_calls_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalChatCallsStats:
    try:
        return await StatsService.get_global_chat_calls_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in chat calls stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/chat/tokens", response_model=GlobalChatTokensStats)  # type: ignore
async def get_chat_tokens_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalChatTokensStats:
    try:
        return await StatsService.get_global_chat_tokens_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in chat tokens stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/chat/users", response_model=GlobalUsersStats)  # type: ignore
async def get_chat_users_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalUsersStats:
    try:
        return await StatsService.get_global_chat_users_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in chat users stats route: {str(e)}", exc_info=True)
        raise


# --- Generic inference stats: one set of routes for api / liberclaw / x402 / cli ---


@router.get("/global/{key_type}/calls", response_model=GlobalApiStats)  # type: ignore
async def get_inference_calls_stats(
    key_type: InferenceKeyType,
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalApiStats:
    try:
        return await StatsService._get_inference_api_stats(ApiKeyType(key_type.value), start_date, end_date)
    except Exception as e:
        logger.error(f"Error in {key_type.value} calls stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/{key_type}/tokens", response_model=GlobalTokensStats)  # type: ignore
async def get_inference_tokens_stats(
    key_type: InferenceKeyType,
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalTokensStats:
    try:
        return await StatsService._get_inference_tokens_stats(ApiKeyType(key_type.value), start_date, end_date)
    except Exception as e:
        logger.error(f"Error in {key_type.value} tokens stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/{key_type}/credits", response_model=GlobalCreditsStats)  # type: ignore
async def get_inference_credits_stats(
    key_type: InferenceKeyType,
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalCreditsStats:
    try:
        return await StatsService._get_inference_credits_stats(ApiKeyType(key_type.value), start_date, end_date)
    except Exception as e:
        logger.error(f"Error in {key_type.value} credits stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/{key_type}/users", response_model=GlobalUsersStats)  # type: ignore
async def get_inference_users_stats(
    key_type: InferenceKeyType,
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalUsersStats:
    try:
        return await StatsService._get_inference_users_stats(ApiKeyType(key_type.value), start_date, end_date)
    except Exception as e:
        logger.error(f"Error in {key_type.value} users stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/users", response_model=GlobalUsersStats)  # type: ignore
async def get_aggregate_users_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalUsersStats:
    try:
        return await StatsService.get_global_users_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in aggregate users stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/summary", response_model=GlobalSummaryStats)  # type: ignore
async def get_global_summary(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalSummaryStats:
    try:
        return await StatsService.get_global_summary_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in global summary stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/messages-by-segment", response_model=GlobalSegmentMessagesStats)  # type: ignore
async def get_messages_by_segment(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalSegmentMessagesStats:
    try:
        return await StatsService.get_global_messages_by_segment(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in messages-by-segment stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/credits-consumption", response_model=GlobalCreditsConsumptionStats)  # type: ignore
async def get_credits_consumption(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalCreditsConsumptionStats:
    try:
        return await StatsService.get_global_credits_consumption(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in credits-consumption stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/subscriptions", response_model=GlobalSubscriptionsStats)  # type: ignore
async def get_subscriptions_stats() -> GlobalSubscriptionsStats:
    try:
        return await StatsService.get_global_subscriptions_stats()
    except Exception as e:
        logger.error(f"Error in subscriptions stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/subscribers-over-time", response_model=GlobalSubscribersOverTimeStats)  # type: ignore
async def get_subscribers_over_time(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalSubscribersOverTimeStats:
    try:
        return await StatsService.get_global_subscribers_over_time(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in subscribers-over-time stats route: {str(e)}", exc_info=True)
        raise
