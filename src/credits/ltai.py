import json
import os

import requests
from web3 import Web3

from src.config import config
from src.credits import router
from src.interfaces.credits import CreditTransactionProvider
from src.models.base import SessionLocal
from src.models.credit_transaction import CreditTransaction
from src.services.credit_service import CreditService
from src.utils.cron import scheduler, ltai_payments_lock
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))


code_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(code_dir, "../abis/LTAIPaymentProcessor.json"), "r") as abi_file:
    PAYMENT_PROCESSOR_CONTRACT_ABI = json.load(abi_file)


@scheduler.scheduled_job("interval", seconds=20)
@router.post("/ltai/process", description="Process credit purchase with $LTAI transactions")  # type: ignore
async def process_ltai_transactions() -> list[str]:
    processed_transactions: list[str] = []

    if ltai_payments_lock.locked():
        return processed_transactions  # Skip execution if already running

    async with ltai_payments_lock:
        contract = w3.eth.contract(address=config.LTAI_PAYMENT_PROCESSOR_CONTRACT, abi=PAYMENT_PROCESSOR_CONTRACT_ABI)

        from_block = get_start_block()

        events = contract.events.PaymentProcessed.get_logs(from_block=from_block)

        for event in events:
            try:
                transaction_hash = handle_payment_event(event)
            except Exception as e:
                logger.error(f"Error processing payment: {e}", exc_info=True)
            processed_transactions.append(transaction_hash)

    return processed_transactions


def handle_payment_event(event) -> str:
    """Handle a PaymentProcessed event from the LTAI Payment Processor contract

    Args:
        event: The event object

    Returns:
        The transaction hash of the event
    """

    logger.debug(f"Processing payment event: {event}")

    transaction_hash = f"0x{event['transactionHash'].hex()}"
    sender = event["args"]["sender"]
    amount = event["args"]["amount"]
    block_number = event["blockNumber"]

    token_price = get_token_price()  # Get token/USD price
    amount = token_price * (amount / 10**18)  # Calculate USD value
    CreditService.add_credits(CreditTransactionProvider.libertai, sender, amount, transaction_hash, block_number)
    return transaction_hash


def get_token_price() -> float:
    """Get the current price of $LTAI in USD from Coingecko"""
    try:
        response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=libertai&vs_currencies=usd")
        response.raise_for_status()  # Raise exception for 4XX/5XX responses
        price_data = response.json()

        if "libertai" not in price_data or "usd" not in price_data["libertai"]:
            logger.error(f"Unexpected response format from Coingecko: {price_data}")
            raise ValueError("Unexpected response format from Coingecko")

        price = price_data["libertai"]["usd"]

        if price is None or price <= 0:
            logger.error(f"Invalid token price received: {price}")
            raise ValueError("Invalid price from Coingecko")

        return price
    except requests.RequestException as e:
        logger.error(f"Failed to fetch token price: {str(e)}")
        raise e


def get_start_block() -> int:
    """
    Get the starting block for transaction processing.
    Returns the block after the most recent transaction in DB or defaults to current block.
    """
    db = SessionLocal()
    try:
        # Find the most recent transaction and get its block number
        last_transaction = (
            db.query(CreditTransaction)
            .filter(CreditTransaction.block_number.isnot(None))
            .order_by(CreditTransaction.block_number.desc())
            .first()
        )

        if last_transaction and last_transaction.block_number is not None:
            # Start from the block after the last processed one
            start_block = last_transaction.block_number + 1
            logger.debug(f"Starting from block {start_block} (last processed block)")
        else:
            # If no transactions with block numbers, start from recent blocks
            start_block = w3.eth.block_number
            logger.debug(f"No previous blocks found, starting from {start_block}")

        return start_block
    finally:
        db.close()
