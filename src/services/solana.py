import base64
import hashlib
import json
import logging
import struct

from solana.rpc.api import Client
from solders.pubkey import Pubkey
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from src.config import config
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.base import SessionLocal
from src.models.credit_transaction import CreditTransaction
from src.services.credit import CreditService
from src.utils.token import get_token_price, get_sol_token_price

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
                .where(
                    CreditTransaction.provider.in_(
                        [CreditTransactionProvider.ltai_solana.value, CreditTransactionProvider.sol_solana.value]
                    )
                )
                .order_by(desc(CreditTransaction.block_number))
                .limit(1)
            )
            result = db.execute(stmt).scalar_one_or_none()
            return result if result else None
        except Exception as e:
            logger.error(f"Error getting last block from DB: {e}")
            return None

    @staticmethod
    def _get_event_discriminator(event_name: str) -> bytes:
        """Calculates the 8-byte Anchor event discriminator."""
        return hashlib.sha256(f"event:{event_name}".encode("utf-8")).digest()[:8]

    def extract_payment_event(self, meta):
        """Extract PaymentEvent or SolPaymentEvent data from transaction metadata"""

        # Calculate event discriminators once
        payment_event_discriminator = self._get_event_discriminator("PaymentEvent")
        sol_payment_event_discriminator = self._get_event_discriminator("SolPaymentEvent")

        status = CreditTransactionStatus.completed if meta.get("err") is None else CreditTransactionStatus.error

        log_messages = meta.get("logMessages", [])
        for msg in log_messages:
            if msg.startswith("Program data: "):
                try:
                    event_data = base64.b64decode(msg[14:])

                    # Check for PaymentEvent discriminator
                    if event_data.startswith(payment_event_discriminator):
                        # Ensure data is long enough
                        if len(event_data) >= 72:
                            offset = 8
                            user = str(Pubkey(event_data[offset : offset + 32]))
                            offset += 32
                            amount = struct.unpack("<Q", event_data[offset : offset + 8])[0]
                            # skip timestamp and token_mint

                            return {"user": user, "amount": amount, "status": status, "event_type": "token_payment"}

                    # Check for SolPaymentEvent discriminator
                    elif event_data.startswith(sol_payment_event_discriminator):
                        # Ensure data is long enough
                        if len(event_data) >= 56:
                            offset = 8
                            user = str(Pubkey(event_data[offset : offset + 32]))
                            offset += 32
                            amount = struct.unpack("<Q", event_data[offset : offset + 8])[0]
                            # skip timestamp

                            return {"user": user, "amount": amount, "status": status, "event_type": "sol_payment"}

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
                signatures = self.client.get_signatures_for_address(self.program_id, limit=50)

                if not signatures.value:
                    return processed_signatures
                new_transactions = []

                for sig_info in signatures.value:
                    signature_str = str(sig_info.signature)

                    existing_tx = db.scalar(
                        select(CreditTransaction).where(
                            CreditTransaction.transaction_hash == signature_str,
                            CreditTransaction.provider.in_(
                                [
                                    CreditTransactionProvider.ltai_solana.value,
                                    CreditTransactionProvider.sol_solana.value,
                                ]
                            ),
                        )
                    )
                    if existing_tx:
                        continue

                    if self.last_processed_slot and sig_info.slot <= self.last_processed_slot:
                        continue
                    new_transactions.append((sig_info, signature_str))

                if not new_transactions:
                    return processed_signatures

                ltai_token_price = get_token_price()
                sol_token_price = get_sol_token_price()

                for sig_info, signature_str in reversed(new_transactions):
                    tx = self.client.get_transaction(
                        sig_info.signature, encoding="json", max_supported_transaction_version=0
                    )
                    if tx.value:
                        await self._process_transaction(
                            tx.value, signature_str, ltai_token_price, sol_token_price, sig_info.slot
                        )
                        self.last_processed_slot = sig_info.slot
                        processed_signatures.append(signature_str)

        except Exception as e:
            logger.error(f"Polling error: {e}")

        return processed_signatures

    async def _process_transaction(
        self, value, signature: str, ltai_token_price: float, sol_token_price: float, slot: int
    ) -> list[str]:
        """Process individual transaction if it contains PaymentEvent or SolPaymentEvent"""
        try:
            tx = value.transaction
            tx_json = json.loads(tx.to_json())
            meta = tx_json["meta"]

            payment_event = self.extract_payment_event(meta)
            if not payment_event:
                return []

            if payment_event["event_type"] == "token_payment":
                amount_after = payment_event["amount"] / (10**9)
                logger.info(f"💰 Token Payment: {amount_after} tokens from {payment_event['user']} | Tx: {signature}")
                amount = ltai_token_price * amount_after
                provider = CreditTransactionProvider.ltai_solana
            elif payment_event["event_type"] == "sol_payment":
                amount_after = payment_event["amount"] / (10**9)
                logger.info(f"💰 SOL Payment: {amount_after} SOL from {payment_event['user']} | Tx: {signature}")
                amount = sol_token_price * amount_after
                provider = CreditTransactionProvider.sol_solana
            else:
                logger.warning(f"Unknown payment event type: {payment_event['event_type']}")
                return []

            CreditService.add_credits(
                provider=provider,
                address=payment_event["user"],
                amount=amount,
                transaction_hash=signature,
                block_number=slot,
                status=payment_event["status"],
            )

        except Exception as e:
            logger.error(f"Error processing transaction: {e}")
        return []
