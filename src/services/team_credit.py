"""Team credit balance: top-ups drained in place, plus the debit ledger.

Mirrors ``CreditService``'s draining algorithm against the team-scoped table.
All methods run on the caller's session and only flush — the caller commits.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.team_credit_transaction import TeamCreditTransaction
from src.models.team_ledger_entry import TeamLedgerEntry
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class TeamCreditService:
    @staticmethod
    async def get_balance(db: AsyncSession, team_id: uuid.UUID) -> float:
        result = await db.execute(
            select(func.coalesce(func.sum(TeamCreditTransaction.amount_left), 0.0)).where(
                TeamCreditTransaction.team_id == team_id,
                TeamCreditTransaction.is_active == True,  # noqa: E712
                TeamCreditTransaction.status == CreditTransactionStatus.completed,
            )
        )
        return float(result.scalar() or 0.0)

    @staticmethod
    async def add_credits(
        db: AsyncSession,
        team_id: uuid.UUID,
        amount: float,
        provider: CreditTransactionProvider,
        external_reference: str | None = None,
        status: CreditTransactionStatus = CreditTransactionStatus.completed,
    ) -> TeamCreditTransaction | None:
        """Record a team top-up. Returns None if ``external_reference`` was already processed."""
        if external_reference:
            existing = (
                await db.execute(
                    select(TeamCreditTransaction.id).where(
                        TeamCreditTransaction.external_reference == external_reference
                    )
                )
            ).scalar_one_or_none()
            if existing:
                logger.warning(f"Team transaction {external_reference} already processed, skipping")
                return None
        tx = TeamCreditTransaction(
            team_id=team_id,
            amount=amount,
            amount_left=amount,
            provider=provider,
            external_reference=external_reference,
            status=status,
        )
        db.add(tx)
        await db.flush()
        return tx

    @staticmethod
    async def use_credits(
        db: AsyncSession, team_id: uuid.UUID, amount: float, allow_partial: bool = False
    ) -> bool:
        """Deduct from the team's completed transactions, oldest first, rows locked.

        Returns True if the full amount was deducted; on shortfall deducts nothing
        unless ``allow_partial`` (then drains what's available and returns False).
        """
        result = await db.execute(
            select(TeamCreditTransaction)
            .where(
                TeamCreditTransaction.team_id == team_id,
                TeamCreditTransaction.is_active == True,  # noqa: E712
                TeamCreditTransaction.status == CreditTransactionStatus.completed,
            )
            .order_by(TeamCreditTransaction.created_at.asc())
            .with_for_update()
        )
        transactions = result.scalars().all()

        available = sum(tx.amount_left for tx in transactions if tx.amount_left > 0)
        fully_deductible = available >= amount
        if not fully_deductible:
            logger.warning(
                f"Insufficient team credits for team {team_id}: requested {amount}, missing {amount - available}"
            )
            if not allow_partial:
                return False

        remaining = amount
        for tx in transactions:
            if tx.amount_left <= 0:
                continue
            take = min(tx.amount_left, remaining)
            tx.amount_left -= take
            remaining -= take
            if remaining <= 0:
                break
        await db.flush()
        return fully_deductible

    @staticmethod
    async def log(
        db: AsyncSession,
        team_id: uuid.UUID,
        entry_type: str,
        amount: float,
        metadata: dict | None = None,
    ) -> None:
        db.add(TeamLedgerEntry(team_id=team_id, entry_type=entry_type, amount=amount, metadata_json=metadata))
        await db.flush()
