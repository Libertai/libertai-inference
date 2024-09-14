from aleph.sdk import AlephHttpClient
from fastapi import APIRouter

from src.config import config
from src.interfaces.hold import HoldPostSubscribeBody, HoldAggregateData

router = APIRouter(tags=["Hold provider"])


@router.post("/hold/subscribe")
async def subscribe(body: HoldPostSubscribeBody):
    aggregates = await fetch_hold_balances()
    return aggregates


async def fetch_hold_balances() -> dict[str, int]:
    async with AlephHttpClient(api_server=config.ALEPH_API_URL) as client:
        result = await client.fetch_aggregates(
            address=config.LTAI_BALANCES_AGGREGATE_SENDER, keys=[config.LTAI_BALANCES_AGGREGATE_KEY]
        )
    balances = HoldAggregateData(tokens=result[config.LTAI_BALANCES_AGGREGATE_KEY])
    return balances.tokens
