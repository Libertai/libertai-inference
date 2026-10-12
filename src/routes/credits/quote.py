from fastapi import Depends

from src.interfaces.credits import TokenPaymentQuoteRequest, TokenPaymentQuoteResponse
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.routes.credits import router
from src.services import token_quote
from src.services.auth import get_current_user


@router.post(  # type: ignore
    "/quotes",
    description="Lock the token price of an on-chain top-up: paying exactly token_amount_raw before expires_at "
    "credits credit_amount",
)
async def create_token_payment_quote(
    request: TokenPaymentQuoteRequest, user: User = Depends(get_current_user)
) -> TokenPaymentQuoteResponse:
    async with AsyncSessionLocal() as db:
        quote = await token_quote.create_quote(db, user, request)
        await db.commit()
    return TokenPaymentQuoteResponse(
        id=str(quote.id),
        chain=request.chain,
        token=request.token,
        payer_address=quote.payer_address,
        credit_amount=quote.credit_amount,
        token_price=quote.token_price,
        token_amount_raw=str(int(quote.token_amount_raw)),
        decimals=token_quote.QUOTABLE_TOKENS[(request.chain, request.token)].decimals,
        expires_at=quote.expires_at,
    )
