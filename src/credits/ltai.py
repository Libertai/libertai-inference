import json
import os

import requests
from web3 import Web3

from src.config import config
from src.credits import router
from src.utils.cron import scheduler, ltai_payments_lock

w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))

LTAI_BASE_ADDRESS = Web3.to_checksum_address(config.LTAI_BASE_ADDRESS)
LTAI_PAYMENT_PROCESSOR_CONTRACT = Web3.to_checksum_address(config.LTAI_PAYMENT_PROCESSOR_CONTRACT)

code_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(code_dir, "../abis/LTAIPaymentProcessor.json"), "r") as abi_file:
    PAYMENT_PROCESSOR_CONTRACT_ABI = json.load(abi_file)


@scheduler.scheduled_job("interval", seconds=20)
@router.post("/ltai/process", description="Process credit purchase with $LTAI transactions")
async def process_ltai_transactions() -> list[str]:
    processed_transactions: list[str] = []

    if ltai_payments_lock.locked():
        return processed_transactions  # Skip execution if already running

    async with ltai_payments_lock:
        contract = w3.eth.contract(address=LTAI_PAYMENT_PROCESSOR_CONTRACT, abi=PAYMENT_PROCESSOR_CONTRACT_ABI)

        # Use a polling approach instead of filters that expire
        # from_block = w3.eth.block_number
        from_block = 27879468
        # TODO: start from the latest block in the credit_transactions table (without forgetting to check transaction hash to avoid duplicates)
        print(f"Starting to watch from block {from_block}")
        # TODO: switch to logger

        try:
            current_block = w3.eth.block_number
            if current_block > from_block:
                print(f"Checking blocks {from_block+1} to {current_block}")
                events = contract.events.PaymentProcessed.get_logs(from_block=from_block + 1, to_block=current_block)

                for event in events:
                    transaction_hash = handle_event(event)
                    processed_transactions.append(transaction_hash)

        except Exception as e:
            print(f"Error occurred: {e}")
    return processed_transactions


def handle_event(event) -> str:
    """Handle a PaymentProcessed event from the LTAI Payment Processor contract

    Args:
        event: The event object

    Returns:
        The transaction hash of the event
    """

    print(f"Processing event: {event}")

    transaction_hash = f"0x{event['transactionHash'].hex()}"
    sender = event["args"]["sender"]
    amount = event["args"]["amount"]
    amount_burned = event["args"]["amountBurned"]

    token_price = get_token_price()  # Get token/USD price
    usd_value = token_price * (amount / 10**18)  # Calculate USD value
    data = {
        "sender": sender,
        "usd_value": usd_value,
        "transaction_hash": transaction_hash,
        "amount_burned": amount_burned / 10**18,
    }
    print(f"Credits API called with {data}")
    return transaction_hash


def get_token_price() -> float:
    """Get the current price of $LTAI in USD from Coingecko"""
    response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=libertai&vs_currencies=usd")
    price = response.json()["libertai"]["usd"]

    if price is None or price <= 0:
        raise ValueError("Invalid price from Coingecko")

    return price
