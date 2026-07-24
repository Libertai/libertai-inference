from uuid import UUID

from fastapi import Depends, HTTPException
from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.blockchain import LibertaiChain

from src.interfaces.credits import (
    CreditTransactionProvider,
    GetVouchersRequest,
    VoucherAddCreditsRequest,
    VoucherChangeExpireRequest,
    VoucherCreditsResponse,
)
from src.models.base import AsyncSessionLocal
from src.routes.credits import router
from src.services.auth import require_staff
from src.services.credit import CreditService
from src.services.users import get_or_create_user_by_email, get_user_by_email
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.post(  # type: ignore
    "/vouchers",
    description="[staff] Add credits via voucher to a wallet address or an email account (created if new)",
    dependencies=[Depends(require_staff)],
)
async def add_voucher_credits(voucher_request: VoucherAddCreditsRequest) -> bool:
    if voucher_request.email is not None:
        # Credit by email — auto-create the account if it doesn't exist yet. No wallet.
        # Commit the (possibly new) user before crediting: add_credits_for_user opens its
        # own session and must see the row.
        async with AsyncSessionLocal() as db:
            user, _ = await get_or_create_user_by_email(db, voucher_request.email)
            await db.commit()
            user_id = user.id
        return await CreditService.add_credits_for_user(
            user_id=user_id,
            amount=voucher_request.amount,
            provider=CreditTransactionProvider.voucher,
            expired_at=voucher_request.expired_at,
        )

    # Wallet recipient: resolve/create the user from the address (unchanged behaviour).
    return await CreditService.add_credits(
        provider=CreditTransactionProvider.voucher,
        address=format_address(voucher_request.chain, voucher_request.address),
        amount=voucher_request.amount,
        expired_at=voucher_request.expired_at,
    )


@router.get(  # type: ignore
    "/vouchers",
    description="[staff] Get all vouchers for a wallet address (with chain) or an email account",
    dependencies=[Depends(require_staff)],
)
async def get_vouchers(
    chain: LibertaiChain | None = None, address: str | None = None, email: str | None = None
) -> list[VoucherCreditsResponse]:
    if email:
        # Resolve the email to an existing user (never create on lookup); email-granted vouchers
        # carry a user_id but no address, so they're only found by user.
        async with AsyncSessionLocal() as db:
            user = await get_user_by_email(db, email.strip())
        vouchers = await CreditService.get_vouchers_for_user(user.id) if user is not None else []
    elif address:
        if chain is None:
            raise HTTPException(status_code=400, detail="A wallet address requires its chain")
        try:
            params = GetVouchersRequest(chain=chain, address=address)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        vouchers = await CreditService.get_vouchers(params.address)
    else:
        raise HTTPException(status_code=400, detail="Provide an email, or a wallet address with its chain")

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


@router.post(  # type: ignore
    "/voucher/expiration",
    description="[staff] Change a voucher's expiration date",
    dependencies=[Depends(require_staff)],
)
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
