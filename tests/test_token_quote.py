"""Token payment quotes: a quoted on-chain payment is credited the quoted amount, once, within its TTL.

Services open their own sessions against the committed test DB; each test uses fresh addresses.
Coingecko is stubbed.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.blockchain import LibertaiChain
from sqlalchemy import select

from src.interfaces.credits import CreditTransactionProvider, QuoteToken, TokenPaymentQuoteRequest
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.token_payment_quote import TokenPaymentQuote
from src.models.user import User
from src.models.wallet_connection import WalletConnection
from src.services import token_quote
from src.services.auth_tokens import create_access_token
from src.services.credit import CreditService

pytestmark = pytest.mark.asyncio

PRICE = 0.5


@pytest.fixture(autouse=True)
def _stub_prices(monkeypatch):
    async def price() -> float:
        return PRICE

    for key, spec in list(token_quote.QUOTABLE_TOKENS.items()):
        monkeypatch.setitem(
            token_quote.QUOTABLE_TOKENS, key, token_quote._QuotableToken(spec.decimals, price, spec.boost)
        )


def _evm() -> str:
    return f"0x{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"


def _ref() -> str:
    return f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"


async def _user(wallet: str | None = None, chain: LibertaiChain = LibertaiChain.base) -> User:
    async with AsyncSessionLocal() as db:
        user = User(address=wallet or _evm())
        db.add(user)
        await db.flush()
        if wallet:
            db.add(WalletConnection(user_id=user.id, chain=chain.value, address=format_address(chain, wallet)))
        await db.commit()
        return user


async def _quote(payer: str, credit_amount: float = 100.0, token: QuoteToken = QuoteToken.ltai) -> TokenPaymentQuote:
    chain = LibertaiChain.base if token == QuoteToken.ltai else LibertaiChain.solana
    user = await _user(payer, chain)
    async with AsyncSessionLocal() as db:
        quote = await token_quote.create_quote(
            db,
            user,
            TokenPaymentQuoteRequest(chain=chain, token=token, payer_address=payer, credit_amount=credit_amount),
        )
        await db.commit()
        return quote


async def _claim(payer: str, raw: int, paid_at, ref: str) -> TokenPaymentQuote | None:
    async with AsyncSessionLocal() as db:
        return await token_quote.claim_quote(db, LibertaiChain.base, QuoteToken.ltai, payer, raw, paid_at, ref)


async def test_raw_amount_accounts_for_ltai_boost():
    # 100 credits / 1.25 boost = $80 of LTAI at $0.5 = 160 LTAI
    quote = await _quote(_evm())
    assert quote.token_amount_raw == Decimal(160 * 10**18)
    assert quote.expires_at - quote.created_at == token_quote.QUOTE_TTL


async def test_sol_quote_has_no_boost():
    quote = await _quote("4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T", token=QuoteToken.sol)
    assert quote.token_amount_raw == Decimal(200 * 10**9)


async def test_quote_requires_a_wallet_linked_to_the_requester():
    user = await _user(_evm())
    async with AsyncSessionLocal() as db:
        with pytest.raises(Exception) as exc:
            await token_quote.create_quote(
                db,
                user,
                TokenPaymentQuoteRequest(
                    chain=LibertaiChain.base, token=QuoteToken.ltai, payer_address=_evm(), credit_amount=10
                ),
            )
    assert getattr(exc.value, "status_code", None) == 403


async def test_ltai_on_solana_quotable_sol_on_base_not():
    user = await _user()
    async with AsyncSessionLocal() as db:
        with pytest.raises(Exception) as exc:
            await token_quote.create_quote(
                db,
                user,
                TokenPaymentQuoteRequest(
                    chain=LibertaiChain.base, token=QuoteToken.sol, payer_address=_evm(), credit_amount=10
                ),
            )
    assert getattr(exc.value, "status_code", None) == 400


async def test_matching_payment_claims_quote_once():
    payer = _evm()
    quote = await _quote(payer)
    raw = int(quote.token_amount_raw)
    paid_at = quote.created_at + timedelta(minutes=5)
    ref = _ref()

    claimed = await _claim(payer, raw, paid_at, ref)
    assert claimed is not None and claimed.id == quote.id
    assert claimed.external_reference == ref and claimed.consumed_at is not None

    # Retry of the same payment returns the same quote; another payment can't reuse it.
    again = await _claim(payer, raw, paid_at, ref)
    assert again is not None and again.id == quote.id
    assert await _claim(payer, raw, paid_at, _ref()) is None


@pytest.mark.parametrize(
    "offset,amount_delta,same_payer",
    [
        (timedelta(minutes=16), 0, True),  # expired
        (-timedelta(minutes=5), 0, True),  # paid before the quote existed
        (timedelta(minutes=1), 1, True),  # amount differs
        (timedelta(minutes=1), 0, False),  # another wallet
    ],
)
async def test_non_matching_payment_falls_back(offset, amount_delta, same_payer):
    payer = _evm()
    quote = await _quote(payer)
    result = await _claim(
        payer if same_payer else _evm(),
        int(quote.token_amount_raw) + amount_delta,
        quote.created_at + offset,
        _ref(),
    )
    assert result is None


async def test_already_credited_payment_does_not_consume_quote():
    payer = _evm()
    quote = await _quote(payer)
    ref = _ref()
    assert await CreditService.add_credits(CreditTransactionProvider.ltai_base, payer, 1.0, ref, block_number=1)

    assert await _claim(payer, int(quote.token_amount_raw), quote.created_at, ref) is None
    async with AsyncSessionLocal() as db:
        assert (await db.get(TokenPaymentQuote, quote.id)).consumed_at is None


async def test_add_credits_without_boost_credits_exact_amount():
    payer = _evm()
    ref = _ref()
    assert await CreditService.add_credits(
        CreditTransactionProvider.ltai_base, payer, 100.0, ref, block_number=1, apply_ltai_boost=False
    )
    async with AsyncSessionLocal() as db:
        tx = (
            await db.execute(select(CreditTransaction).where(CreditTransaction.external_reference == ref))
        ).scalar_one()
    assert tx.amount == 100.0


async def test_quote_route(async_client):
    payer = "0x52908400098527886E0F7030069857D2E4169EE7"
    user = await _user(payer)
    body = {"chain": "base", "token": "ltai", "payer_address": payer, "credit_amount": 100}

    assert (await async_client.post("/credits/quotes", json=body)).status_code == 401
    bad = {**body, "payer_address": "nope"}
    auth = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    assert (await async_client.post("/credits/quotes", json=bad, headers=auth)).status_code == 422

    res = await async_client.post("/credits/quotes", json=body, headers=auth)
    assert res.status_code == 200
    data = res.json()
    assert data["token_amount_raw"] == str(160 * 10**18)
    assert data["decimals"] == 18
    assert data["payer_address"] == payer.lower()
    assert data["expires_at"].endswith("Z")
