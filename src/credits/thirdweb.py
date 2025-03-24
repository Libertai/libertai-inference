from typing import Any, Dict, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field
from web3 import Web3

from src.config import config
from src.credits import router
from src.interfaces.credits import CreditTransactionProvider, ThirdwebBuyWithCryptoWebhook
from src.services.credit_service import CreditService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class ThirdwebWebhookPayload(BaseModel):
    data: Dict[str, Any] = Field(...)

    @property
    def buy_with_crypto_status(self) -> Optional[ThirdwebBuyWithCryptoWebhook]:
        if "buyWithCryptoStatus" in self.data:
            return ThirdwebBuyWithCryptoWebhook(**self.data["buyWithCryptoStatus"])
        return None


@router.post("/thirdweb/webhook", description="Receive webhooks from Thirdweb")
async def thirdweb_webhook(payload: ThirdwebWebhookPayload) -> None:
    """
    Process webhooks from Thirdweb.
    Currently only supports buyWithCryptoStatus events.
    """
    logger.debug(f"Received Thirdweb webhook: {payload.model_dump_json()}")

    data = payload.buy_with_crypto_status
    if data is None:
        raise HTTPException(status_code=400, detail="Unsupported webhook type")

    if data.status != "COMPLETED" or data.destination is None:
        logger.info(f"Ignoring non-completed transaction: {data.status}")
        return

    if Web3.to_checksum_address(data.toAddress) != config.LTAI_PAYMENT_PROCESSOR_CONTRACT:
        logger.warning(f"Transaction not destined for LTAI payment processor ({data.toAddress}), ignoring it")
        return

    try:
        # Extract transaction details
        transaction_hash = data.destination.transactionHash
        sender_address = data.fromAddress

        # Convert amount from cents to dollars
        amount_usd = data.destination.amountUSDCents / 100

        # Add credits to the user's account
        CreditService.add_credits(
            provider=CreditTransactionProvider.thirdweb,
            address=sender_address,
            amount=amount_usd,
            transaction_hash=transaction_hash,
            block_number=None,  # Thirdweb doesn't provide block number
        )

        logger.info(
            f"Added {amount_usd} USD worth of credits to {sender_address} "
            f"from Thirdweb transaction {transaction_hash}"
        )

    except Exception as e:
        logger.error(f"Error processing Thirdweb webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing webhook: {str(e)}")
