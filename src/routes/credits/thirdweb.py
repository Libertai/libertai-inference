import hashlib
import hmac
import time
import uuid
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from web3 import Web3

from src.config import config
from src.interfaces.credits import (
    CreditTransactionProvider,
    CreditTransactionStatus,
    ThirdwebOnchainTransactionData,
    ThirdwebOnrampTransactionData,
    ThirdwebPurchaseData,
)
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.routes.credits import router
from src.services.credit import CreditService
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Maximum age of webhook in seconds before rejecting it (5 minutes)
MAX_WEBHOOK_AGE = 300


class ThirdwebWebhookPayload(BaseModel):
    version: int
    type: str
    data: dict[str, Any]


@router.post("/thirdweb/webhook", description="Receive webhooks from Thirdweb")  # type: ignore
async def thirdweb_webhook(request: Request) -> None:
    signature = request.headers.get("X-Pay-Signature") or request.headers.get("X-Payload-Signature")
    timestamp = request.headers.get("X-Pay-Timestamp") or request.headers.get("X-Timestamp")

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

        if current_time - webhook_timestamp > MAX_WEBHOOK_AGE:
            logger.warning(f"Webhook timestamp too old: {webhook_timestamp}, current time: {current_time}")
            raise HTTPException(status_code=401, detail="Webhook expired")

        if webhook_timestamp > current_time + 30:
            logger.warning(f"Webhook timestamp from the future: {webhook_timestamp}, current time: {current_time}")
            raise HTTPException(status_code=401, detail="Invalid timestamp")
    except ValueError:
        logger.warning(f"Invalid timestamp format: {timestamp}")
        raise HTTPException(status_code=401, detail="Invalid timestamp format")

    # Get raw request body for signature verification
    body = await request.body()
    body_str = body.decode("utf-8")

    signature_payload = f"{timestamp}.{body_str}"

    expected_signature = hmac.new(
        config.THIRDWEB_WEBHOOK_SECRET.encode(), signature_payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        logger.warning("Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    logger.debug(f"Received Thirdweb webhook: {body_str}")

    # Parsed here rather than as a route argument so an unreadable payload can be logged in full.
    try:
        payload = ThirdwebWebhookPayload.model_validate_json(body)
        if payload.version < 2:
            logger.warning(f"Ignoring retired v{payload.version} webhook: {body_str}")
            return
        if payload.type == "pay.onchain-transaction":
            data: ThirdwebOnchainTransactionData | ThirdwebOnrampTransactionData = (
                ThirdwebOnchainTransactionData.model_validate(payload.data)
            )
        elif payload.type == "pay.onramp-transaction":
            data = ThirdwebOnrampTransactionData.model_validate(payload.data)
        else:
            logger.debug(f"Ignoring unsupported webhook type: {payload.type}")
            return
    except ValidationError as e:
        logger.error(f"Unreadable Thirdweb webhook payload: {e}. Body: {body_str}")
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    if isinstance(data, ThirdwebOnchainTransactionData):
        await _handle_onchain_transaction(data)
    else:
        await _handle_onramp_transaction(data)


def _credit_status(status: str) -> CreditTransactionStatus | None:
    """None for a terminal non-success (FAILED, REFUNDED, ...): nothing to credit."""
    if status == "COMPLETED":
        return CreditTransactionStatus.completed
    if status == "PENDING":
        return CreditTransactionStatus.pending
    return None


async def _credit_thirdweb_purchase(
    purchase: ThirdwebPurchaseData, amount_usd: float, external_reference: str, status: CreditTransactionStatus
) -> None:
    """Credit a Thirdweb purchase to the signed-in user's account (the wallet that paid may not be
    the user's own — email/OAuth users pay via a just-connected wallet)."""
    await CreditService.add_credits_for_user(
        user_id=uuid.UUID(purchase.userId),
        amount=amount_usd,
        provider=CreditTransactionProvider.thirdweb,
        external_reference=external_reference,
        status=status,
    )


async def _handle_onchain_transaction(data: ThirdwebOnchainTransactionData) -> None:
    if Web3.to_checksum_address(data.receiver) != config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE:
        logger.warning(f"Transaction not destined for LTAI payment processor ({data.receiver}), ignoring it")
        return

    try:
        base_transaction = next((tx for tx in data.transactions[::-1] if tx.chainId == 8453), None)
        if base_transaction is None:
            logger.warning("No Base chain transaction found in onchain transaction")
            return

        external_reference = base_transaction.transactionHash

        amount_usd = int(data.destinationAmount) / (10**data.destinationToken.decimals)

        if data.destinationToken.symbol != "USDC":
            logger.warning(f"Unsupported destination token: {data.destinationToken.symbol}")
            return
        if data.destinationToken.chainId != 8453:
            logger.warning(f"Unsupported destination token chain: {data.destinationToken.chainId}")
            return

        tx_status = _credit_status(data.status)
        if tx_status is None:
            logger.warning(f"Ignoring onchain transaction {external_reference} with status {data.status}")
            return

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CreditTransaction).where(CreditTransaction.external_reference == external_reference)
            )
            existing_transaction = result.scalars().first()

        if existing_transaction is not None:
            if tx_status == CreditTransactionStatus.completed:
                await CreditService.update_transaction_status(external_reference, tx_status)
            return

        await _credit_thirdweb_purchase(data.purchaseData, amount_usd, external_reference, tx_status)

    except Exception as e:
        logger.error(f"Error processing Thirdweb onchain webhook: {e!s}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing onchain webhook: {e!s}")


async def _handle_onramp_transaction(data: ThirdwebOnrampTransactionData) -> None:
    if Web3.to_checksum_address(data.receiver) != config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE:
        logger.warning(f"Onramp transaction not destined for LTAI payment processor ({data.receiver}), ignoring it")
        return

    try:
        external_reference = data.id

        amount_usd = int(data.amount) / (10**data.token.decimals)

        if data.token.symbol != "USDC":
            logger.warning(f"Unsupported onramp token: {data.token.symbol}")
            return
        if data.token.chainId != 8453:
            logger.warning(f"Unsupported onramp token chain: {data.token.chainId}")
            return

        tx_status = _credit_status(data.status)
        if tx_status is None:
            logger.warning(f"Ignoring onramp transaction {external_reference} with status {data.status}")
            return

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CreditTransaction).where(CreditTransaction.external_reference == external_reference)
            )
            existing_transaction = result.scalars().first()

        if existing_transaction is not None:
            if tx_status == CreditTransactionStatus.completed:
                await CreditService.update_transaction_status(external_reference, tx_status)
            return

        await _credit_thirdweb_purchase(data.purchaseData, amount_usd, external_reference, tx_status)

    except Exception as e:
        logger.error(f"Error processing Thirdweb onramp webhook: {e!s}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing onramp webhook: {e!s}")
