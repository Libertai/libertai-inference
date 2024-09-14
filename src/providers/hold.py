from http import HTTPStatus

from aleph.sdk import AlephHttpClient
from fastapi import APIRouter, HTTPException

from src.config import config
from src.interfaces.hold import HoldPostSubscribeBody, HoldAggregateData
from src.interfaces.subscription import SubscriptionType

router = APIRouter(tags=["Hold provider"])

# TODO: update these placeholder prices
ltai_hold_prices: dict[SubscriptionType, int] = {SubscriptionType.standard: 1000}


@router.post("/hold/subscribe")
async def subscribe(body: HoldPostSubscribeBody):
    all_balances = await fetch_hold_balances()
    address = body.account.address.upper()
    balance = all_balances.get(address, None)
    required_hold_amount = ltai_hold_prices.get(body.type, None)

    if balance is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail=f"Address {body.account.address} not found in holders list"
        )

    # TODO: check if existing subscriptions with hold tier that would reduce available amounts
    if required_hold_amount is None or balance < required_hold_amount:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail=f"Not enough tokens held to pay this subscription (held and available {balance}, required {required_hold_amount})",
        )

    return balance


async def fetch_hold_balances() -> dict[str, int]:
    async with AlephHttpClient(api_server=config.ALEPH_API_URL) as client:
        result = await client.fetch_aggregates(
            address=config.LTAI_BALANCES_AGGREGATE_SENDER, keys=[config.LTAI_BALANCES_AGGREGATE_KEY]
        )
    balances = HoldAggregateData(tokens=result[config.LTAI_BALANCES_AGGREGATE_KEY])
    return {k.upper(): v for k, v in balances.tokens.items()}
