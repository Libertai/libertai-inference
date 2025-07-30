import json
import logging

import base64
import struct

from solana.rpc.api import Client
from sqlalchemy import select, desc
from sqlalchemy.orm import Session
from solders.pubkey import Pubkey


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
        self.last_processed_slot = None

    @staticmethod
    def _get_last_block_from_db(db: Session) -> int | None:
        """Get the last processed block from database"""
        try:
            stmt = (
                select(CreditTransaction.block_number)
                .where(CreditTransaction.provider == CreditTransactionProvider.solana)
                .order_by(desc(CreditTransaction.block_number))
                .limit(1)
            )
            result = db.execute(stmt).scalar_one_or_none()
            return result if result else None
        except Exception as e:
            logger.error(f"Error getting last block from DB: {e}")
            return None

    def extract_payment_event(self, meta):
        """Extract PaymentEvent data from transaction metadata"""

        status = CreditTransactionStatus.completed if meta.get("err") is None else CreditTransactionStatus.error

        log_messages = meta.get("logMessages", [])
        for msg in log_messages:
            # Anchor events are logged as "Program data: <base64_data>"
            if msg.startswith("Program data: "):
                try:
                    event_data = base64.b64decode(msg[14:])  # Remove "Program data: " prefix
                    
                    # Check if this is a PaymentEvent (first 8 bytes are discriminator)
                    # PaymentEvent discriminator can be computed from event name hash
                    if len(event_data) >= 72:  # 8 (discriminator) + 32 (user) + 8 (amount) + 8 (timestamp) + 32 (token_mint)
                        # Skip discriminator (first 8 bytes) and parse the event data
                        offset = 8
                        
                        # Parse user (32 bytes)
                        user_bytes = event_data[offset:offset+32]
                        user = str(Pubkey(user_bytes))
                        offset += 32
                        
                        # Parse amount (8 bytes, little endian)
                        amount = struct.unpack('<Q', event_data[offset:offset+8])[0]
                        offset += 8
                        
                        # Skip timestamp (8 bytes, little endian)
                        offset += 8
                        
                        return {
                            "user": user,
                            "amount": amount,
                            "status": status,
                        }
                except Exception as e:
                    print(f"Error parsing event data: {e}")
                    continue
        
        return None

    async def poll_transactions(self) -> list[str]:
        """Poll for new transactions"""
        processed_signatures: list[str] = []

        try:
            with SessionLocal() as db:
                self.last_processed_slot = self._get_last_block_from_db(db)

                # Get recent transactions
                signatures = self.client.get_signatures_for_address(
                    self.program_id,
                    limit=50
                )

                if not signatures.value:
                    return processed_signatures
                new_transactions = []
                token_price = get_token_price()

                for sig_info in signatures.value:
                    signature_str = str(sig_info.signature)

                    existing_tx = db.scalar(
                        select(CreditTransaction).where(
                            CreditTransaction.transaction_hash == signature_str,
                            CreditTransaction.provider == CreditTransactionProvider.solana
                        )
                    )
                    if existing_tx:
                        continue

                    if self.last_processed_slot and sig_info.slot <= self.last_processed_slot:
                        continue
                    new_transactions.append((sig_info, signature_str))

                for sig_info, signature_str in reversed(new_transactions):
                    tx = self.client.get_transaction(
                        sig_info.signature,
                        encoding="json",
                        max_supported_transaction_version=0
                    )
                    if tx.value:
                        await self._process_transaction(tx.value, signature_str, token_price, sig_info.slot)
                        self.last_processed_slot = sig_info.slot

        except Exception as e:
            logger.error(f"Polling error: {e}")

        return processed_signatures

    async def _process_transaction(self, value, signature: str, token_price: float, slot: int) -> list[str]:
        """Process individual transaction if it contains PaymentEvent"""
        try:
            tx = value.transaction
            tx_json = json.loads(tx.to_json())
            meta = tx_json["meta"]

            payment_event = self.extract_payment_event(meta)
            if not payment_event:
               return []
            amount_after = payment_event['amount'] / (10 ** 9)
            print(f"💰 Payment: {amount_after} tokens from {payment_event['user']} | Tx: {signature}")
            amount = token_price * amount_after

            CreditService.add_credits(
                provider=CreditTransactionProvider.solana,
                address=payment_event["user"],
                amount=amount,
                transaction_hash=signature,
                block_number=slot,
                status=payment_event["status"],
            )

        except Exception as e:
            logger.error(f"Error processing transaction: {e}")
        return []
