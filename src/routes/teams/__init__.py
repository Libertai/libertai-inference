from fastapi import APIRouter

router = APIRouter(prefix="/teams", tags=["Teams"])

# Import handlers so they register on the router.
from src.routes.teams import admin, teams  # noqa: E402,F401
