from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from fastapi import HTTPException, status
from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.blockchain import LibertaiChain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.credits import QuoteToken, TokenPaymentQuoteRequest
from src.models.credit_transaction import CreditTransaction
from src.models.token_payment_quote import TokenPaymentQuote
from src.models.user import User
from src.models.wallet_connection import WalletConnection
from src.services.credit import LTAI_PAYMENT_BOOST
from src.utils.logger import setup_logger
from src.utils.token import get_sol_token_price, get_token_price

logger = setup_logger(__name__)

QUOTE_TTL = timedelta(minutes=15)
# Tolerated lead of the server clock over block timestamps.
CLOCK_SKEW = timedelta(minutes=1)


@dataclass(frozen=True)
class _QuotableToken:
    decimals: int
    get_price: Callable[[], Awaitable[float]]
    # Credits granted per USD paid.
    boost: float


QUOTABLE_TOKENS: dict[tuple[LibertaiChain, QuoteToken], _QuotableToken] = {
    (LibertaiChain.base, QuoteToken.ltai): _QuotableToken(18, get_token_price, LTAI_PAYMENT_BOOST),
    (LibertaiChain.solana, QuoteToken.ltai): _QuotableToken(9, get_token_price, LTAI_PAYMENT_BOOST),
    (LibertaiChain.solana, QuoteToken.sol): _QuotableToken(9, get_sol_token_price, 1.0),
}


def utcnow() -> datetime:
    """Naive UTC, matching the schema's TIMESTAMP columns whatever the host's TZ."""
    return datetime.now(UTC).replace(tzinfo=None)


def raw_token_amount(credit_amount: float, price: float, spec: _QuotableToken) -> Decimal:
    """Smallest-unit amount worth ``credit_amount`` credits at ``price``, rounded up."""
    tokens = Decimal(str(credit_amount)) / Decimal(str(spec.boost)) / Decimal(str(price))
    return (tokens * (Decimal(10) ** spec.decimals)).to_integral_value(rounding=ROUND_CEILING)


async def create_quote(db: AsyncSession, user: User, request: TokenPaymentQuoteRequest) -> TokenPaymentQuote:
    spec = QUOTABLE_TOKENS.get((request.chain, request.token))
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{request.token.value} payments are not supported on {request.chain.value}",
        )
    payer_address = format_address(request.chain, request.payer_address)
    # Only the account a wallet signs in as can lock a price for its payments.
    owned = (
        await db.execute(
            select(WalletConnection.id).where(
                WalletConnection.user_id == user.id,
                WalletConnection.chain == request.chain.value,
                WalletConnection.address == payer_address,
            )
        )
    ).first()
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="The paying wallet must be linked to your account"
        )
    price = await spec.get_price()
    now = utcnow()
    quote = TokenPaymentQuote(
        user_id=user.id,
        chain=request.chain.value,
        token=request.token.value,
        payer_address=payer_address,
        credit_amount=request.credit_amount,
        token_price=price,
        token_amount_raw=raw_token_amount(request.credit_amount, price, spec),
        created_at=now,
        expires_at=now + QUOTE_TTL,
    )
    db.add(quote)
    await db.flush()
    return quote


async def claim_quote(
    db: AsyncSession,
    chain: LibertaiChain,
    token: QuoteToken,
    payer_address: str,
    token_amount_raw: int,
    paid_at: datetime,
    external_reference: str,
) -> TokenPaymentQuote | None:
    """The quote this payment fulfils, marked consumed by it; None to value it at the market price.

    Idempotent per payment: a retry returns the quote it already consumed. Commits.
    """
    already_credited = (
        await db.execute(
            select(CreditTransaction.id).where(CreditTransaction.external_reference == external_reference)
        )
    ).first()
    if already_credited is not None:
        return None

    consumed = (
        (await db.execute(select(TokenPaymentQuote).where(TokenPaymentQuote.external_reference == external_reference)))
        .scalars()
        .first()
    )
    if consumed is not None:
        return consumed

    quote = (
        (
            await db.execute(
                select(TokenPaymentQuote)
                .where(
                    TokenPaymentQuote.chain == chain.value,
                    TokenPaymentQuote.token == token.value,
                    TokenPaymentQuote.payer_address == payer_address,
                    TokenPaymentQuote.token_amount_raw == Decimal(token_amount_raw),
                    TokenPaymentQuote.consumed_at.is_(None),
                    TokenPaymentQuote.created_at <= paid_at + CLOCK_SKEW,
                    TokenPaymentQuote.expires_at >= paid_at,
                )
                .order_by(TokenPaymentQuote.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if quote is None:
        return None

    quote.consumed_at = utcnow()
    quote.external_reference = external_reference
    await db.commit()
    logger.info(f"Payment {external_reference} fulfils quote {quote.id} for {quote.credit_amount} credits")
    return quote
