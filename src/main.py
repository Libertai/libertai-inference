import asyncio

from aleph.sdk.client import AlephHttpClient

from config import config


async def main():
    async with AlephHttpClient(api_server=config.ALEPH_API_URL) as client:
        aggregates = await client.fetch_aggregates(
            address=config.LTAI_BALANCES_AGGREGATE_SENDER, keys=[config.LTAI_BALANCES_AGGREGATE_KEY]
        )
        print(aggregates)


asyncio.run(main())
