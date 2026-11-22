import base64
import json
import math

import httpx

from src.config import config
from src.interfaces.aleph import AudioPricing, EmbeddingPricing, TextPricing
from src.services.aleph import aleph_service
from src.utils.cdp_auth import CDP_HOST, cdp_auth_headers
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

FACILITATOR_ROUTE = "/platform/v2/x402"


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
            elif "embedding" in model.pricing:
                embedding_pricing = model.pricing["embedding"]
                if not isinstance(embedding_pricing, EmbeddingPricing):
                    continue
                prices[model.id] = {
                    "price_per_million_input_tokens": embedding_pricing.price_per_million_input_tokens,
                    "is_embedding": True,
                }
            elif "audio" in model.pricing:
                audio_pricing = model.pricing["audio"]
                if not isinstance(audio_pricing, AudioPricing):
                    continue
                prices[model.id] = {
                    "price_per_million_input_characters": audio_pricing.price_per_million_input_characters,
                    "is_audio": True,
                }
            elif "image" in model.pricing:
                image_price = model.pricing["image"]
                if not isinstance(image_price, (int, float)):
                    continue
                prices[model.id] = {
                    "price_per_image": float(image_price),
                }

        return prices

    @staticmethod
    def settlement_requirements(payment_requirements: dict, actual_amount: float) -> dict:
        """Requirements to settle with: upto charges the actual cost, floored at
        X402_MIN_PRICE and capped at the authorized maximum; exact charges what was signed."""
        if payment_requirements.get("scheme") != "upto":
            return payment_requirements
        authorized = int(payment_requirements["amount"])
        charged = math.ceil(round(max(actual_amount, config.X402_MIN_PRICE) * 1_000_000, 6))
        return {**payment_requirements, "amount": str(min(charged, authorized))}

    @staticmethod
    async def settle_payment(payment_payload: str, payment_requirements: str, actual_amount: float) -> bool:
        """Settle a standard x402 v2 payment through the CDP facilitator.

        payment_payload is the client's base64 PAYMENT-SIGNATURE header, payment_requirements
        the JSON requirements it signed (its `accepted`), both relayed by the model server.
        """
        try:
            payload = json.loads(base64.b64decode(payment_payload))
            requirements = X402Service.settlement_requirements(json.loads(payment_requirements), actual_amount)
            path = f"{FACILITATOR_ROUTE}/settle"
            headers = cdp_auth_headers(config.CDP_API_KEY_ID, config.CDP_API_KEY_SECRET, "POST", path)
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"https://{CDP_HOST}{path}",
                    json={"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirements},
                    headers=headers,
                )
            if response.status_code == 200 and response.json().get("success"):
                logger.info(f"x402 payment settled ({requirements['amount']} USDC atomic units)")
                return True
            # Served but unpaid, e.g. the payer moved funds after /verify: one stable,
            # greppable line per occurrence to spot abusive payers.
            authorization = payload.get("payload", {})
            authorization = authorization.get("authorization") or authorization.get("permit2Authorization") or {}
            logger.error(
                f"x402 settlement failed: payer={authorization.get('from')} scheme={requirements.get('scheme')} "
                f"amount={requirements.get('amount')} status={response.status_code} response={response.text}"
            )
            return False
        except Exception as e:
            logger.error(f"x402 settlement exception: {e}", exc_info=True)
            return False


x402_service = X402Service()
