import time

import aiohttp

from src.interfaces.aleph import AlephAPIResponse, ModelInfo
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class AlephService:
    __last_fetch_time = 0
    __cache_ttl = 300  # 5 minutes
    __models_data: AlephAPIResponse | None = None
    __api_url = (
        "https://api2.aleph.im/api/v0/aggregates/0xe1F7220D201C64871Cefb25320a8a588393eE508.json?keys=LTAI_PRICING"
    )

    async def __fetch_models_data(self) -> AlephAPIResponse:
        """Fetch models data from Aleph API"""
        current_time = time.time()

        # Return cached data if it's still valid
        if self.__models_data is not None and (current_time - self.__last_fetch_time) < self.__cache_ttl:
            logger.debug("Using cached Aleph models data")
            return self.__models_data

        logger.debug("Fetching fresh Aleph models data")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.__api_url) as response:
                    response.raise_for_status()
                    data = await response.json()
                    parsed_data = AlephAPIResponse.model_validate(data)

                    # Update cache
                    self._models_data = parsed_data
                    self._last_fetch_time = current_time

                    return parsed_data
        except Exception as e:
            logger.error(f"Error fetching Aleph models data: {str(e)}", exc_info=True)
            # If we have cached data, return it even if expired
            if self._models_data is not None:
                logger.warning("Using expired cached data due to fetch error")
                return self._models_data
            # Re-raise if we have no cached data
            raise

    async def get_model_info(self, model_id: str) -> ModelInfo | None:
        """Get information for a specific model by ID"""
        data = await self.__fetch_models_data()

        # Navigate through the updated structure
        models_response = data.data.get("LTAI_PRICING", None)
        if not models_response:
            logger.error("LTAI_PRICING key not found in Aleph API response")
            return None

        for model in models_response.models:
            if model.id == model_id:
                return model

        return None

    async def calculate_price(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        """
        Calculate the price for a given model, input tokens and output tokens

        Args:
            model_id: The ID of the model to use
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Price in credits

        Raises:
            ValueError: If the model ID is invalid or pricing information is unavailable
        """
        model = await self.get_model_info(model_id)

        if not model:
            raise ValueError(f"Invalid model ID: {model_id}")

        if "text" not in model.pricing:
            raise ValueError(f"Pricing information unavailable for model: {model_id}")

        pricing = model.pricing["text"]

        # Calculate price based on tokens
        input_price = input_tokens / 1_000_000 * pricing.price_per_million_input_tokens
        output_price = output_tokens / 1_000_000 * pricing.price_per_million_output_tokens

        total_price = input_price + output_price

        # Round to 5 decimal places for consistency
        return round(total_price, 5)


aleph_service = AlephService()
