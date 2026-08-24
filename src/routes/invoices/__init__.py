from fastapi import APIRouter

router = APIRouter(prefix="/invoices", tags=["Invoices"])

# Import handlers so they register on the router.
from src.routes.invoices import invoices  # noqa: F401
