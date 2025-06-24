import json
import logging
import aiohttp
from solana.rpc.api import Client
from solders.rpc.responses import RpcConfirmedTransactionStatusWithSignature
from sqlalchemy import select, desc
from sqlalchemy.orm import Session
from src.config import config
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import SessionLocal
from src.models.credit_transaction import CreditTransaction
from src.services.credit import CreditService
from src.utils.token import get_token_price

logger = logging.getLogger(__name__)

class SolanaService:
    def __init__(self):
        self.program_id = config.LTAI_PAYMENT_PROCESSOR_CONTRACT_SOLANA
        self.client = Client(config.SOLANA_RPC_URL)

    def _get_last_signature_from_db(self, db: Session) -> str | None:
        """Get the last processed signature from database"""
        try:
            stmt = (
                select(CreditTransaction.transaction_hash)
                .where(CreditTransaction.provider == CreditTransactionProvider.solana)
                .order_by(desc(CreditTransaction.created_at))
                .limit(1)
            )
            result = db.execute(stmt).scalar_one_or_none()
            return result if result else None
        except Exception as e:
            logger.error(f"Error getting last signature from DB: {e}")
            return None

    async def poll_transactions(self) -> list[str]:
        """Poll for new transactions"""
        processed_txs: list[str] = []

        try:
            with SessionLocal() as db:
                last_signature = self._get_last_signature_from_db(db)

                # Get recent transactions
                response = self.client.get_signatures_for_address(self.program_id, limit=1000)

                if not response.value:
                    return processed_txs

                new_signatures = self._filter_new_signatures(response.value, last_signature)

                # Process transactions
                for sig_info in new_signatures:
                    signature_str = str(sig_info.signature)

                    tx_response = self.client.get_transaction(
                        sig_info.signature, encoding="json", max_supported_transaction_version=0
                    )

                    if tx_response.value:
                        processed_sigs = await self._process_transaction(tx_response.value, signature_str)
                        processed_txs.extend(processed_sigs)

        except Exception as e:
            logger.error(f"Polling error: {e}")

        return processed_txs

    def _filter_new_signatures(
        self, signatures: list[RpcConfirmedTransactionStatusWithSignature], last_signature: str | None
    ) -> list:
        """Filter out already processed signatures"""
        if not last_signature:
            return signatures

        new_signatures = []
        for sig_info in signatures:
            if str(sig_info.signature) == last_signature:
                break
            new_signatures.append(sig_info)
        return new_signatures

    async def _process_transaction(
        self, tx_data, signature: str
    ) -> list[str]:
        """Process individual transaction"""
        try:
            # Safely parse transaction data
            if not hasattr(tx_data, "transaction"):
                logger.warning(f"Transaction {signature} has no transaction data")
                return []

            tx_data_json = json.loads(tx_data.to_json())
            tx_block_slot = tx_data_json.get("slot", 0)
            tx_json = json.loads(tx_data.transaction.to_json())
            transaction = tx_json.get("transaction", {})
            meta = tx_json.get("meta", {})
            tx_status_obj = meta.get("status", {})
            message = transaction.get("message", {})
            account_keys = message.get("accountKeys", [])

            if not account_keys:
                logger.warning(f"Transaction {signature} has no account keys")
                return []

            sender = account_keys[0]

            # Process token balance changes
            ltai_amount = self._calculate_token_transfer_amount(meta)

            tx_status = (
                CreditTransactionStatus.completed
                if "Ok" in tx_status_obj
                else CreditTransactionStatus.error
                if "Err" in tx_status_obj
                else CreditTransactionStatus.pending
            )

            if ltai_amount > 0:
                try:
                    token_price = get_token_price()
                    amount = token_price * ltai_amount
                    CreditService.add_credits(
                        provider=CreditTransactionProvider.solana,
                        address=sender,
                        amount=amount,
                        transaction_hash=signature,
                        block_number=tx_block_slot,
                        status=tx_status,
                    )
                except Exception as e:
                    logger.error(f"Error storing transaction in DB: {e}")
                    return []

        except Exception as e:
            logger.error(f"Error processing transaction {signature}: {e}")

        return []

    def _calculate_token_transfer_amount(self, meta: dict) -> float:
        """Calculate the amount of tokens transferred"""
        try:
            pre_balances = {
                b["accountIndex"]: int(b["uiTokenAmount"]["amount"]) for b in meta.get("preTokenBalances", [])
            }
            post_balances = {
                b["accountIndex"]: int(b["uiTokenAmount"]["amount"]) for b in meta.get("postTokenBalances", [])
            }

            for index in pre_balances:
                if index in post_balances:
                    diff = pre_balances[index] - post_balances[index]
                    if diff > 0:
                        # Get decimals from pre_token_balances
                        decimals = None
                        for balance in meta.get("preTokenBalances", []):
                            if balance["accountIndex"] == index:
                                decimals = balance["uiTokenAmount"]["decimals"]
                                break

                        if decimals is not None:
                            return diff / (10**decimals)

        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Error calculating token transfer amount: {e}")

        return 0.0
