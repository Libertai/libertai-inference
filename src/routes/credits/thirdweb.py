import hashlib
import hmac
import time

from fastapi import HTTPException, Header, Request
from libertai_utils.chains.index import format_address
from libertai_utils.interfaces.blockchain import LibertaiChain
from pydantic import BaseModel
from sqlalchemy import select
from web3 import Web3

from src.config import config
from src.interfaces.credits import (
    CreditTransactionProvider,
    ThirdwebOnchainTransactionData,
    ThirdwebOnrampTransactionData,
    CreditTransactionStatus,
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
    data: ThirdwebOnchainTransactionData | ThirdwebOnrampTransactionData

    @property
    def is_onchain_transaction(self) -> bool:
        return self.type == "pay.onchain-transaction"

    @property
    def is_onramp_transaction(self) -> bool:
        return self.type == "pay.onramp-transaction"

    @property
    def onchain_data(self) -> ThirdwebOnchainTransactionData | None:
        if self.is_onchain_transaction:
            return self.data  # type: ignore
        return None

    @property
    def onramp_data(self) -> ThirdwebOnrampTransactionData | None:
        if self.is_onramp_transaction:
            return self.data  # type: ignore
        return None


@router.post("/thirdweb/webhook", description="Receive webhooks from Thirdweb")  # type: ignore
async def thirdweb_webhook(
    request: Request,
    payload: ThirdwebWebhookPayload,
    signature: str = Header(None, alias="X-Pay-Signature"),
    timestamp: str = Header(None, alias="X-Pay-Timestamp"),
) -> None:
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

    logger.debug(f"Received Thirdweb webhook: {payload.model_dump_json()}")

    if payload.is_onchain_transaction:
        await _handle_onchain_transaction(payload.onchain_data)
    elif payload.is_onramp_transaction:
        await _handle_onramp_transaction(payload.onramp_data)
    else:
        logger.debug(f"Ignoring unsupported webhook type: {payload.type}")


async def _handle_onchain_transaction(data: ThirdwebOnchainTransactionData | None) -> None:
    if data is None:
        raise HTTPException(status_code=400, detail="Missing onchain transaction data")

    if Web3.to_checksum_address(data.receiver) != config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE:
        logger.warning(f"Transaction not destined for LTAI payment processor ({data.receiver}), ignoring it")
        return

    try:
        base_transaction = next((tx for tx in data.transactions[::-1] if tx.chainId == 8453), None)
        if base_transaction is None:
            logger.warning("No Base chain transaction found in onchain transaction")
            return

        transaction_hash = base_transaction.transactionHash
        sender_address = data.purchaseData.userAddress

        amount_usd = int(data.destinationAmount) / (10**data.destinationToken.decimals)

        if data.destinationToken.symbol != "USDC":
            logger.warning(f"Unsupported destination token: {data.destinationToken.symbol}")
            return
        if data.destinationToken.chainId != 8453:
            logger.warning(f"Unsupported destination token chain: {data.destinationToken.chainId}")
            return

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CreditTransaction).where(CreditTransaction.transaction_hash == transaction_hash)
            )
            existing_transaction = result.scalars().first()

        tx_status = (
            CreditTransactionStatus.completed if data.status == "COMPLETED" else CreditTransactionStatus.pending
        )

        if existing_transaction is not None:
            await CreditService.update_transaction_status(transaction_hash, CreditTransactionStatus.completed)
            return

        await CreditService.add_credits(
            provider=CreditTransactionProvider.thirdweb,
            address=format_address(LibertaiChain.base, sender_address),
            amount=amount_usd,
            transaction_hash=transaction_hash,
            block_number=None,
            status=tx_status,
        )

    except Exception as e:
        logger.error(f"Error processing Thirdweb onchain webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing onchain webhook: {str(e)}")


async def _handle_onramp_transaction(data: ThirdwebOnrampTransactionData | None) -> None:
    if data is None:
        raise HTTPException(status_code=400, detail="Missing onramp transaction data")

    if Web3.to_checksum_address(data.receiver) != config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE:
        logger.warning(f"Onramp transaction not destined for LTAI payment processor ({data.receiver}), ignoring it")
        return

    try:
        transaction_hash = data.id
        sender_address = data.purchaseData.userAddress

        amount_usd = int(data.amount) / (10**data.token.decimals)

        if data.token.symbol != "USDC":
            logger.warning(f"Unsupported onramp token: {data.token.symbol}")
            return
        if data.token.chainId != 8453:
            logger.warning(f"Unsupported onramp token chain: {data.token.chainId}")
            return

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CreditTransaction).where(CreditTransaction.transaction_hash == transaction_hash)
            )
            existing_transaction = result.scalars().first()

        tx_status = (
            CreditTransactionStatus.completed if data.status == "COMPLETED" else CreditTransactionStatus.pending
        )

        if existing_transaction is not None:
            if data.status == "COMPLETED":
                await CreditService.update_transaction_status(transaction_hash, CreditTransactionStatus.completed)
            return

        await CreditService.add_credits(
            provider=CreditTransactionProvider.thirdweb,
            address=format_address(LibertaiChain.base, sender_address),
            amount=amount_usd,
            transaction_hash=transaction_hash,
            block_number=None,
            status=tx_status,
        )

    except Exception as e:
        logger.error(f"Error processing Thirdweb onramp webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing onramp webhook: {str(e)}")
