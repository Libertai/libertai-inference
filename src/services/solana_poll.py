import logging
import json
from solana.rpc.api import Client
from solders.pubkey import Pubkey
from src.config import config

logger = logging.getLogger(__name__)

PROGRAM_ID="2RHgoS9Xdx8DcA9aCPzK9afQUJfZGip7w1VU4VkiTp2P"
RPC_ENDPOINT = "https://api.testnet.solana.com"

class TransactionPoller:
    def __init__(self):
        self.program_id = config.LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA
        self.client = Client(config.SOLANA_RPC_URL)
        self.last_signature = None

    async def poll_transactions(self) -> list[str]:
        """Poll for new transactions every x seconds"""
        processed_txs: list[str] = []
        try:
            print("Polling..")
            # Get recent transactions for the program
            signatures = self.client.get_signatures_for_address(
                self.program_id,
                limit=10
            )
            if signatures.value:
                for sig_info in signatures.value:
                    signature_str = str(sig_info.signature)
                    # TODO: handle it in the db directly
                    if signature_str == self.last_signature:
                        break

                    tx = self.client.get_transaction(
                        sig_info.signature,
                        encoding="json",
                        max_supported_transaction_version=0
                    )

                    if tx.value:
                        processed_txs = await self.process_transaction(tx.value, signature_str)

                # Update last processed signature
                if signatures.value:
                    self.last_signature = str(signatures.value[0].signature)
        except Exception as e:
            logger.error(f"Polling error: {e}")
        return processed_txs

    async def process_transaction(self, value, signature: str) -> list[str]:
        processed_txs: list[str] = []
        try:
            tx = value.transaction
            tx_json = json.loads(tx.to_json())
            meta = tx_json["meta"]
            transaction = tx_json["transaction"]
            sender = transaction["message"]["accountKeys"][0]

            pre_balances = {b["accountIndex"]: int(b["uiTokenAmount"]["amount"]) for b in meta.get("preTokenBalances", [])}
            post_balances = {b["accountIndex"]: int(b["uiTokenAmount"]["amount"]) for b in meta.get("postTokenBalances", [])}

            for index in pre_balances:
                if index in post_balances:
                    diff = pre_balances[index] - post_balances[index]
                    if diff > 0:
                        amount_sent = diff / (10 ** meta["preTokenBalances"][0]["uiTokenAmount"]["decimals"])
                        processed_txs.append(signature)
                        print(f"🧾 Tx Signature: {signature}")
                        print(f"📤 Sender Address: {sender}")
                        print(f"💸 Sent {amount_sent} tokens to program {self.program_id}")
        except Exception as e:
            logger.error(f"Error processing transaction: {e}")
        return processed_txs
