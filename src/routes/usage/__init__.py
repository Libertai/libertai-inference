from fastapi import APIRouter

router = APIRouter(prefix="/usage", tags=["Usage"])

# Import handlers so they register on the router.
from src.routes.usage import usage  # noqa: F401
