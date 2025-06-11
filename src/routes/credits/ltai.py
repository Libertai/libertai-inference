import json
import os

import requests
from web3 import Web3

from src.config import config
from src.interfaces.credits import CreditTransactionProvider
from src.routes.credits import router
from src.services.credit import CreditService
from src.utils.cron import scheduler, ltai_payments_lock
from src.utils.logger import setup_logger
from src.services.solana_poll import TransactionPoller

logger = setup_logger(__name__)
poller = TransactionPoller()

w3 = Web3(Web3.HTTPProvider(config.BASE_RPC_URL))


code_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(code_dir, "../../abis/LTAIPaymentProcessor.json"), "r") as abi_file:
    PAYMENT_PROCESSOR_CONTRACT_ABI = json.load(abi_file)


@scheduler.scheduled_job("interval", seconds=60)
@router.post("/ltai/base/process", description="Process credit purchase with $LTAI transactions in Base")  # type: ignore
async def process_base_ltai_transactions() -> list[str]:
    processed_transactions: list[str] = []

    if ltai_payments_lock.locked():
        return processed_transactions  # Skip execution if already running

    async with ltai_payments_lock:
        contract = w3.eth.contract(address=config.LTAI_PAYMENT_PROCESSOR_CONTRACT_BASE, abi=PAYMENT_PROCESSOR_CONTRACT_ABI)

        # Start from recent blocks with a margin to include missed blocks between executions or downtimes
        from_block = w3.eth.block_number - 1000

        events = contract.events.PaymentProcessed.get_logs(from_block=from_block)

        for event in events:
            try:
                transaction_hash = handle_payment_event(event)
            except Exception as e:
                logger.error(f"Error processing payment: {e}", exc_info=True)
            processed_transactions.append(transaction_hash)

    return processed_transactions

@scheduler.scheduled_job("interval", seconds=100)
@router.post("/ltai/solana/process", description="Process credit purchase with $LTAI in solana blockchain") # type: ignore
async def process_solana_ltai_transactions() -> list[str]:
    processed_transactions: list[str] = []
    if ltai_payments_lock.locked():
        return processed_transactions  # Skip execution if already running
    processed_transactions = await poller.poll_transactions()
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
