from fastapi import APIRouter

router = APIRouter(prefix="/credits", tags=["Credits"])

# Import routes
from src.routes.credits.ltai import process_ltai_transactions  # noqa
from src.routes.credits.thirdweb import thirdweb_webhook  # noqa
from src.routes.credits.general import update_expired_credit_transactions  # noqa
from src.routes.credits.voucher import add_voucher_credits  # noqa
