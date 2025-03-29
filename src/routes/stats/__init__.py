from fastapi import APIRouter

router = APIRouter(prefix="/stats", tags=["Statistics"])

from src.routes.stats.stats import get_dashboard_stats  # noqa
