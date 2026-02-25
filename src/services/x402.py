from src.interfaces.aleph import TextPricing
from src.models.base import SessionLocal
from src.models.user import User
from src.models.x402_balance import X402Balance
from src.services.aleph import aleph_service
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class X402Service:
    @staticmethod
    def update_balance(db, payer_address: str, payment_amount: float, actual_cost: float):
        """Upsert x402 balance for a payer address."""
        # Get or create user
        user = db.query(User).filter(User.address == payer_address).first()
        if not user:
            user = User(address=payer_address)
            db.add(user)
            db.flush()

        balance_record = db.query(X402Balance).filter(X402Balance.user_address == payer_address).first()
        if balance_record:
            balance_record.balance += payment_amount - actual_cost
            balance_record.request_count += 1
        else:
            balance_record = X402Balance(  # type: ignore
                user_address=payer_address,
                balance=(payment_amount - actual_cost),
                request_count=1,
            )
            db.add(balance_record)
        db.commit()

    @staticmethod
    async def get_current_prices() -> dict[str, float]:
        """Return base x402 prices per model. No adjustment — balances are reconciled periodically."""
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
                # Base price: estimate for ~1000 tokens in + 500 out
                base_price = (
                    1000 / 1_000_000 * pricing.price_per_million_input_tokens
                    + 500 / 1_000_000 * pricing.price_per_million_output_tokens
                )
                base_price = max(base_price, 0.0001)
            elif "image" in model.pricing:
                image_price = model.pricing["image"]
                if isinstance(image_price, TextPricing):
                    continue
                base_price = float(image_price)
            else:
                continue

            prices[model.id] = round(base_price, 6)

        return prices

    @staticmethod
    def get_balances() -> list[dict]:
        """Return all x402 user balances for admin reconciliation."""
        with SessionLocal() as db:
            records = db.query(X402Balance).all()
            return [
                {
                    "user_address": r.user_address,
                    "balance": r.balance,
                    "request_count": r.request_count,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in records
            ]


x402_service = X402Service()
