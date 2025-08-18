from datetime import date

from fastapi import Depends, Query

from src.interfaces.stats import DashboardStats, UsageStats, GlobalCreditsStats, GlobalApiStats, GlobalAgentStats
from src.routes.stats import router
from src.services.auth import get_current_address
from src.services.stats import StatsService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.get("/dashboard", response_model=DashboardStats)  # type: ignore
async def get_dashboard_stats(user_address: str = Depends(get_current_address)) -> DashboardStats:
    """
    Get dashboard statistics for the authenticated user:
    - Credits used per month for the last 6 months
    - Number of inference calls made this month
    - Tokens used this month (input, output, and total)
    """
    try:
        return StatsService.get_dashboard_stats(user_address)
    except Exception as e:
        logger.error(f"Error in dashboard stats route for {user_address}: {str(e)}", exc_info=True)
        raise


@router.get("/usage", response_model=UsageStats)  # type: ignore
async def get_usage_stats(
        start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
        end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
        user_address: str = Depends(get_current_address),
) -> UsageStats:
    """
    Get detailed usage statistics for a specific date range.

    Statistics include:
    - Total number of inference calls
    - Total tokens (input and output)
    - Total cost
    - Daily breakdown of token usage
    - Usage breakdown by model
    - Usage breakdown by API key
    """
    try:
        return StatsService.get_usage_stats(user_address, start_date, end_date)
    except Exception as e:
        logger.error(f"Error in usage stats route for {user_address}: {str(e)}", exc_info=True)
        raise

@router.get("/global/credits", response_model=GlobalCreditsStats) # type: ignore
async def get_credits_stats(
        start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
        end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalCreditsStats:
    """
        Get detailed credits statistics and models usage for a specific date range.

        Statistics include:
        - Credits used per model
        - Tokens used per model
        - Which model has been used
    """

    try:
        return StatsService.get_global_credits_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in credits stats route: {str(e)}", exc_info=True)
        raise

@router.get("/global/api", response_model=GlobalApiStats) # type: ignore
async def get_api_stats(
        start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
        end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalApiStats:
    """
        Get detailed api statistics and models usage for a specific date range.

        Statistics include:
        - Total API calls for the entire models
        - List with API calls for each model
    """

    try:
        return StatsService.get_global_api_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in credits stats route: {str(e)}", exc_info=True)
        raise

@router.get("/global/agents", response_model=GlobalAgentStats) # type: ignore
async def get_agent_stats(
        start_date: date = Query(..., description="Start date in format YYYY-MM-DD"),
        end_date: date = Query(..., description="End date in format YYYY-MM-DD"),
) -> GlobalAgentStats:
    """
        Get detailed agent usage statistics for a specific date range.

        Statistics include:
        - Total agents created
        - Total given vouchers
        - Total subscriptions to the agents
        - List with the agents creations dates
    """
    try:
        return StatsService.get_global_agent_stats(start_date, end_date)
    except Exception as e:
        logger.error(f"Error in agent stats route: {str(e)}", exc_info=True)
        raise