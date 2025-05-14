from uuid import UUID

from fastapi import HTTPException

from src.interfaces.credits import (
    VoucherAddCreditsRequest,
    CreditTransactionProvider,
    VoucherCreditsResponse,
    GetVouchersRequest,
    VoucherChangeExpireRequest,
)
from src.routes.credits import router
from src.services.credit import CreditService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.post("/vouchers", description="Add credits via voucher to a specific address")  # type: ignore
async def add_voucher_credits(voucher_request: VoucherAddCreditsRequest) -> bool:
    # Add credits using the voucher provider
    success = CreditService.add_credits(
        provider=CreditTransactionProvider.voucher,
        address=voucher_request.address,
        amount=voucher_request.amount,
        expired_at=voucher_request.expired_at,
    )

    return success


@router.get("/vouchers", description="Get all vouchers for a specific address")  # type: ignore
async def get_vouchers(address: str, password: str) -> list[VoucherCreditsResponse]:
    # Validate input using Pydantic model
    try:
        params = GetVouchersRequest(address=address, password=password)
    except ValueError as e:
        # This explicitly raises the validation error to be handled by FastAPI
        raise HTTPException(status_code=400, detail=str(e))

    # Get all vouchers for the address
    vouchers = CreditService.get_vouchers(params.address)

    # Convert to response model
    return [
        VoucherCreditsResponse(
            id=str(voucher.id),
            address=voucher.address,
            amount=voucher.amount,
            amount_left=voucher.amount_left,
            expired_at=voucher.expired_at,
            created_at=voucher.created_at,
            is_active=voucher.is_active,
        )
        for voucher in vouchers
    ]


@router.post("/voucher/expiration", description="Change a voucher's expiration date")  # type: ignore
async def change_voucher_expiration(request: VoucherChangeExpireRequest) -> bool:
    # Convert string to UUID to ensure it's valid
    try:
        voucher_id = str(UUID(request.voucher_id))
    except ValueError:
        logger.warning(f"Invalid voucher ID format: {request.voucher_id}")
        return False

    # Mark voucher as expired
    success = CreditService.change_voucher_expiration_date(voucher_id, request.expired_at)
    return success
