from fastapi import APIRouter

router = APIRouter(prefix="/credits", tags=["Credits"])

# Import routes
from src.credits.ltai import process_ltai_transactions  # noqa
from src.credits.thirdweb import thirdweb_webhook  # noqa
