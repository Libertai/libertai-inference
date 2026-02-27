import json

import aiohttp

from src.config import config
from src.interfaces.aleph import TextPricing
from src.services.aleph import aleph_service
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

THIRDWEB_X402_BASE = "https://api.thirdweb.com/v1/payments/x402"


class X402Service:
    @staticmethod
    async def get_current_prices() -> dict[str, dict]:
        """Return x402 prices from LTAI_PRICING aggregate."""
        data = aleph_service.models_data
        if data is None:
            return {}
        models_response = data.data.get("LTAI_PRICING", None)
        if not models_response:
            return {}

        prices = {}
        for model in models_response.models:
            if "text" in model.pricing:
                pricing = model.pricing["text"]
                if not isinstance(pricing, TextPricing):
                    continue
                default_max_tokens = 8192
                if "text" in model.capabilities:
                    cap = model.capabilities["text"]
                    if hasattr(cap, "context_window"):
                        default_max_tokens = cap.context_window
                prices[model.id] = {
                    "price_per_million_input_tokens": pricing.price_per_million_input_tokens,
                    "price_per_million_output_tokens": pricing.price_per_million_output_tokens,
                    "default_max_tokens": default_max_tokens,
                }
            elif "image" in model.pricing:
                image_price = model.pricing["image"]
                if isinstance(image_price, TextPricing):
                    continue
                prices[model.id] = {
                    "price_per_image": float(image_price),
                }

        return prices

    @staticmethod
    async def settle_payment(payment_payload: str, payment_requirements: str, actual_amount: float) -> bool:
        """Settle x402 payment via thirdweb facilitator.

        For upto scheme, overrides maxAmountRequired in paymentRequirements with
        the actual usage cost so thirdweb only settles what was consumed.
        """
        try:
            parsed_payload = json.loads(payment_payload) if isinstance(payment_payload, str) else payment_payload
            parsed_requirements = (
                json.loads(payment_requirements) if isinstance(payment_requirements, str) else payment_requirements
            )

            x402_version = parsed_payload.get("x402Version", 2)

            # Override maxAmountRequired with actual cost (USD → micro-USDC)
            actual_amount_micro = str(int(actual_amount * 1_000_000))
            parsed_requirements["maxAmountRequired"] = actual_amount_micro

            headers = {
                "Content-Type": "application/json",
                "x-secret-key": config.THIRDWEB_SECRET_KEY,
            }
            if config.THIRDWEB_VAULT_ACCESS_TOKEN:
                headers["x-vault-access-token"] = config.THIRDWEB_VAULT_ACCESS_TOKEN

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{THIRDWEB_X402_BASE}/settle",
                    json={
                        "x402Version": x402_version,
                        "paymentPayload": parsed_payload,
                        "paymentRequirements": parsed_requirements,
                        "waitUntil": "confirmed",
                    },
                    headers=headers,
                ) as response:
                    if response.status == 200:
                        logger.info(f"x402 payment settled (actual: {actual_amount_micro} micro-USDC)")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"x402 settlement failed: {response.status} - {error_text}")
                        return False
        except Exception as e:
            logger.error(f"x402 settlement exception: {e}")
            return False


x402_service = X402Service()
