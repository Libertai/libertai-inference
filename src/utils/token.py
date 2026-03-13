import httpx

from src.utils.logger import setup_logger

logger = setup_logger(__name__)

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3/simple/price"


async def get_token_price() -> float:
    """Get the current price of $LTAI in USD from Coingecko"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COINGECKO_BASE_URL}?ids=libertai&vs_currencies=usd")
            response.raise_for_status()
            price_data = response.json()

        if "libertai" not in price_data or "usd" not in price_data["libertai"]:
            logger.error(f"Unexpected response format from Coingecko: {price_data}")
            raise ValueError("Unexpected response format from Coingecko")

        price = price_data["libertai"]["usd"]

        if price is None or price <= 0:
            logger.error(f"Invalid token price received: {price}")
            raise ValueError("Invalid price from Coingecko")

        return price
    except httpx.HTTPError as e:
        logger.error(f"Failed to fetch token price: {str(e)}")
        raise


async def get_sol_token_price() -> float:
    """Get the current price of $SOL in USD from Coingecko"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COINGECKO_BASE_URL}?ids=solana&vs_currencies=usd")
            response.raise_for_status()
            price_data = response.json()

        if "solana" not in price_data or "usd" not in price_data["solana"]:
            logger.error(f"Unexpected response format from Coingecko: {price_data}")
            raise ValueError("Unexpected response format from Coingecko")

        price = price_data["solana"]["usd"]

        if price is None or price <= 0:
            logger.error(f"Invalid token price received: {price}")
            raise ValueError("Invalid price from Coingecko")

        return price
    except httpx.HTTPError as e:
        logger.error(f"Failed to fetch token price: {str(e)}")
        raise
