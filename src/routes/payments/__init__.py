from fastapi import APIRouter

router = APIRouter(prefix="/payments", tags=["Payments"])

# Import handlers so they register on the router.
from src.routes.payments import payments  # noqa: E402,F401
