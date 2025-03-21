import json

import requests
from web3 import Web3

from src.config import config
from src.credits import router
from src.utils.cron import scheduler, ltai_payments_lock

w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))

LTAI_BASE_ADDRESS = Web3.to_checksum_address(config.LTAI_BASE_ADDRESS)
LTAI_PAYMENT_RECEIVER_ADDRESS = Web3.to_checksum_address(config.LTAI_PAYMENT_RECEIVER_ADDRESS)
ERC20_ABI = '[{"anonymous":false,"inputs":[{"indexed":true,"name":"from","type":"address"},{"indexed":true,"name":"to","type":"address"},{"indexed":false,"name":"value","type":"uint256"}],"name":"Transfer","type":"event"}]'


@scheduler.scheduled_job("interval", seconds=20)
@router.post("/ltai/process", description="Process credit purchase with $LTAI transactions")
async def process_ltai_transactions() -> None:
    if ltai_payments_lock.locked():
        return  # Skip execution if already running

    async with ltai_payments_lock:
        contract = w3.eth.contract(address=LTAI_BASE_ADDRESS, abi=json.loads(ERC20_ABI))

        # Use a polling approach instead of filters that expire
        latest_block = w3.eth.block_number
        # TODO: start from the latest block in the credit_transactions table (without forgetting to check transaction hash to avoid duplicates)
        print(f"Starting to watch from block {latest_block}")
        # TODO: switch to logger

        try:
            current_block = w3.eth.block_number
            if current_block > latest_block:
                print(f"Checking blocks {latest_block+1} to {current_block}")
                # Get events from the latest_block+1 to current_block
                # 27800774
                events = contract.events.Transfer.get_logs(from_block=latest_block + 1, to_block=current_block)  # type: ignore

                for event in events:
                    handle_event(event)

        except Exception as e:
            print(f"Error occurred: {e}")
    # TODO: return transaction hashes


def handle_event(event):
    transaction_hash = f"0x{event['transactionHash'].hex()}"

    from_address = event["args"]["from"]
    to_address = event["args"]["to"]
    amount = event["args"]["value"]

    if to_address.lower() == LTAI_PAYMENT_RECEIVER_ADDRESS.lower():
        print(f"Received {amount} tokens from {from_address}")
        token_price = get_token_price()  # Get token/USD price
        usd_value = token_price * (amount / 10**18)  # Calculate USD value
        data = {"address": from_address, "usd_value": usd_value, transaction_hash: transaction_hash}
        print(f"Credits API called with {data}")


def get_token_price() -> float:
    """Get the current price of $LTAI in USD from Coingecko"""
    response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=libertai&vs_currencies=usd")
    price = response.json()["libertai"]["usd"]

    if price is None or price <= 0:
        raise ValueError("Invalid price from Coingecko")

    return price
