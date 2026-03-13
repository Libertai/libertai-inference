from datetime import date

from fastapi import Depends, Query

from src.interfaces.stats import (
    DashboardStats,
    UsageStats,
    GlobalCreditsStats,
    GlobalApiStats,
    GlobalTokensStats,
    GlobalChatCallsStats,
    GlobalChatTokensStats,
    GlobalSummaryStats,
)
from src.routes.stats import router
from src.services.auth import get_current_address
from src.services.stats import StatsService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.get("/dashboard", response_model=DashboardStats)  # type: ignore
async def get_dashboard_stats(user_address: str = Depends(get_current_address)) -> DashboardStats:
    try:
        return await StatsService.get_dashboard_stats(user_address)
    except Exception as e:
        logger.error(f"Error in dashboard stats route for {user_address}: {str(e)}", exc_info=True)
        raise


@router.get("/usage", response_model=UsageStats)  # type: ignore
async def get_usage_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
    user_address: str = Depends(get_current_address),
) -> UsageStats:
    try:
        return await StatsService.get_usage_stats(user_address, start_date, end_date)
    except Exception as e:
        logger.error(f"Error in usage stats route for {user_address}: {str(e)}", exc_info=True)
        raise


@router.get("/global/api/credits", response_model=GlobalCreditsStats)  # type: ignore
async def get_credits_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalCreditsStats:
    try:
        return await StatsService.get_global_credits_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in credits stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/api/calls", response_model=GlobalApiStats)  # type: ignore
async def get_api_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalApiStats:
    try:
        return await StatsService.get_global_api_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in credits stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/api/tokens", response_model=GlobalTokensStats)  # type: ignore
async def get_tokens_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalTokensStats:
    try:
        return await StatsService.get_global_tokens_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in token stats route: {str(e)}", exc_info=True)
        raise


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


@router.get("/global/liberclaw/calls", response_model=GlobalApiStats)  # type: ignore
async def get_liberclaw_calls_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalApiStats:
    try:
        return await StatsService.get_global_liberclaw_calls_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in liberclaw calls stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/liberclaw/tokens", response_model=GlobalTokensStats)  # type: ignore
async def get_liberclaw_tokens_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalTokensStats:
    try:
        return await StatsService.get_global_liberclaw_tokens_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in liberclaw tokens stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/liberclaw/credits", response_model=GlobalCreditsStats)  # type: ignore
async def get_liberclaw_credits_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalCreditsStats:
    try:
        return await StatsService.get_global_liberclaw_credits_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in liberclaw credits stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/x402/calls", response_model=GlobalApiStats)  # type: ignore
async def get_x402_calls_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalApiStats:
    try:
        return await StatsService.get_global_x402_calls_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in x402 calls stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/x402/tokens", response_model=GlobalTokensStats)  # type: ignore
async def get_x402_tokens_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalTokensStats:
    try:
        return await StatsService.get_global_x402_tokens_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in x402 tokens stats route: {str(e)}", exc_info=True)
        raise


@router.get("/global/x402/credits", response_model=GlobalCreditsStats)  # type: ignore
async def get_x402_credits_stats(
    start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
    end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalCreditsStats:
    try:
        return await StatsService.get_global_x402_credits_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in x402 credits stats route: {str(e)}", exc_info=True)
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
