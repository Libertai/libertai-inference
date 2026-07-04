from uuid import UUID

from fastapi import HTTPException
from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.blockchain import LibertaiChain
from sqlalchemy import select

from src.interfaces.credits import (
    VoucherAddCreditsRequest,
    CreditTransactionProvider,
    VoucherCreditsResponse,
    GetVouchersRequest,
    VoucherChangeExpireRequest,
)
from src.models.base import AsyncSessionLocal
from src.models.wallet_connection import WalletConnection
from src.routes.credits import router
from src.services.credit import CreditService
from src.services.teams import TeamService
from src.services.users import get_user_by_email
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Team members hold no personal credits — vouchers to them are refused (mirrors /payments/topup).
_MEMBER_BLOCKED = "Team members cannot hold personal credits — use your team balance"


@router.post("/vouchers", description="Add credits via voucher to a wallet address or an email account")  # type: ignore
async def add_voucher_credits(voucher_request: VoucherAddCreditsRequest) -> bool:
    if voucher_request.email is not None:
        # Existing email account only — never create one from a voucher. Credit by user id, no wallet.
        async with AsyncSessionLocal() as db:
            user = await get_user_by_email(db, voucher_request.email)
            if user is None:
                raise HTTPException(status_code=404, detail="No account found for this email")
            if await TeamService.get_membership(db, user.id) is not None:
                raise HTTPException(status_code=400, detail=_MEMBER_BLOCKED)
        return await CreditService.add_credits_for_user(
            user_id=user.id,
            amount=voucher_request.amount,
            provider=CreditTransactionProvider.voucher,
            expired_at=voucher_request.expired_at,
        )

    # Wallet recipient: block an existing member before crediting (a brand-new address
    # has no user/membership yet, so it falls through to the unchanged create+credit path).
    address = format_address(voucher_request.chain, voucher_request.address)
    async with AsyncSessionLocal() as db:
        wallet = (
            await db.execute(select(WalletConnection).where(WalletConnection.address == address))
        ).scalar_one_or_none()
        if wallet is not None and await TeamService.get_membership(db, wallet.user_id) is not None:
            raise HTTPException(status_code=400, detail=_MEMBER_BLOCKED)

    return await CreditService.add_credits(
        provider=CreditTransactionProvider.voucher,
        address=address,
        amount=voucher_request.amount,
        expired_at=voucher_request.expired_at,
    )


@router.get("/vouchers", description="Get all vouchers for a specific address")  # type: ignore
async def get_vouchers(chain: LibertaiChain, address: str, password: str) -> list[VoucherCreditsResponse]:
    # Validate input using Pydantic model
    try:
        params = GetVouchersRequest(chain=chain, address=address, password=password)
    except ValueError as e:
        # This explicitly raises the validation error to be handled by FastAPI
        raise HTTPException(status_code=400, detail=str(e))

    # Get all vouchers for the address
    vouchers = await CreditService.get_vouchers(params.address)

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
    success = await CreditService.change_voucher_expiration_date(voucher_id, request.expired_at)
    return success
