from fastapi import Depends

from src.interfaces.stats import DashboardStats
from src.routes.stats import router
from src.services.auth import get_current_address
from src.services.stats import StatsService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.get("/dashboard", response_model=DashboardStats)
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
