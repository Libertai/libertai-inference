import hashlib
import hmac
import time
from typing import Any

from fastapi import HTTPException, Header, Request
from libertai_utils.chains.ethereum import format_eth_address
from pydantic import BaseModel, Field
from web3 import Web3

from src.config import config
from src.interfaces.credits import CreditTransactionProvider, ThirdwebBuyWithCryptoWebhook, CreditTransactionStatus
from src.models.base import SessionLocal
from src.models.credit_transaction import CreditTransaction
from src.routes.credits import router
from src.services.credit import CreditService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Maximum age of webhook in seconds before rejecting it (5 minutes)
MAX_WEBHOOK_AGE = 300


class ThirdwebWebhookPayload(BaseModel):
    data: dict[str, Any] = Field(...)

    @property
    def buy_with_crypto_status(self) -> ThirdwebBuyWithCryptoWebhook | None:
        if "buyWithCryptoStatus" in self.data:
            return ThirdwebBuyWithCryptoWebhook(**self.data["buyWithCryptoStatus"])
        return None


@router.post("/thirdweb/webhook", description="Receive webhooks from Thirdweb")  # type: ignore
async def thirdweb_webhook(
    request: Request,
    payload: ThirdwebWebhookPayload,
    signature: str = Header(None, alias="X-Pay-Signature"),
    timestamp: str = Header(None, alias="X-Pay-Timestamp"),
) -> None:
    """
    Process webhooks from Thirdweb.
    Currently only supports buyWithCryptoStatus events.

    Verifies the webhook signature using the THIRDWEB_WEBHOOK_SECRET and validates
    the timestamp to prevent replay attacks.
    """
    # Verify the webhook signature
    if not signature:
        logger.warning("Missing signature header in webhook request")
        raise HTTPException(status_code=401, detail="Missing signature")

    # Check timestamp to prevent replay attacks
    if not timestamp:
        logger.warning("Missing timestamp header in webhook request")
        raise HTTPException(status_code=401, detail="Missing timestamp")

    try:
        webhook_timestamp = int(timestamp)
        current_time = int(time.time())

        # Check if webhook is too old
        if current_time - webhook_timestamp > MAX_WEBHOOK_AGE:
            logger.warning(f"Webhook timestamp too old: {webhook_timestamp}, current time: {current_time}")
            raise HTTPException(status_code=401, detail="Webhook expired")

        # Check if webhook is from the future (with a small tolerance)
        if webhook_timestamp > current_time + 30:
            logger.warning(f"Webhook timestamp from the future: {webhook_timestamp}, current time: {current_time}")
            raise HTTPException(status_code=401, detail="Invalid timestamp")
    except ValueError:
        logger.warning(f"Invalid timestamp format: {timestamp}")
        raise HTTPException(status_code=401, detail="Invalid timestamp format")

    # Get raw request body for signature verification
    body = await request.body()
    body_str = body.decode("utf-8")

    # Combine timestamp and body to create the payload for signature verification
    signature_payload = f"{timestamp}.{body_str}"

    # Calculate expected signature
    expected_signature = hmac.new(
        config.THIRDWEB_WEBHOOK_SECRET.encode(), signature_payload.encode(), hashlib.sha256
    ).hexdigest()

    # Secure comparison to prevent timing attacks
    if not hmac.compare_digest(expected_signature, signature):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    logger.debug(f"Received Thirdweb webhook: {payload.model_dump_json()}")

    data = payload.buy_with_crypto_status
    if data is None:
        raise HTTPException(status_code=400, detail="Unsupported webhook type")

    if data.destination is None:
        logger.debug(f"Ignoring transaction without destination: {data.status}")
        return

    if Web3.to_checksum_address(data.toAddress) != config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE:
        logger.warning(f"Transaction not destined for LTAI payment processor ({data.toAddress}), ignoring it")
        return

    try:
        # Extract transaction details
        transaction_hash = data.source.transactionHash
        sender_address = data.purchaseData.userAddress

        # Convert amount from cents to dollars
        amount_usd = data.destination.amountUSDCents / 100

        # First, check if transaction already exists
        with SessionLocal() as db:
            existing_transaction = (
                db.query(CreditTransaction).filter(CreditTransaction.transaction_hash == transaction_hash).first()
            )

        # Determine transaction status based on the webhook status
        tx_status = (
            CreditTransactionStatus.completed if data.status == "COMPLETED" else CreditTransactionStatus.pending
        )

        # If the transaction already exists and status is completed, update it
        if existing_transaction is not None:
            CreditService.update_transaction_status(transaction_hash, CreditTransactionStatus.completed)
            return

        # Add credits to the user's account with the appropriate status
        CreditService.add_credits(
            provider=CreditTransactionProvider.thirdweb,
            address=format_eth_address(sender_address),
            amount=amount_usd,
            transaction_hash=transaction_hash,
            block_number=None,  # Thirdweb doesn't provide block number
            status=tx_status,
        )

    except Exception as e:
        logger.error(f"Error processing Thirdweb webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing webhook: {str(e)}")
