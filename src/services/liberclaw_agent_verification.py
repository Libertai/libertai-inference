"""Read-side status for the $1 LiberClaw agent-verification orders."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.interfaces.liberclaw import LiberclawAgentVerificationStatusResponse
from src.models.liberclaw_agent_verification import LiberclawAgentVerification


async def get_agent_verification_status(
    db: AsyncSession, liberclaw_account_id: uuid.UUID
) -> LiberclawAgentVerificationStatusResponse:
    """Report any paid order, falling back to the latest order for detail.

    Checkout refuses to open a new order while any paid row exists, so a paid
    order must win even when a newer pending row shadows it by created_at.
    """
    paid = (
        await db.execute(
            select(LiberclawAgentVerification)
            .where(
                LiberclawAgentVerification.liberclaw_account_id == liberclaw_account_id,
                LiberclawAgentVerification.status == "paid",
            )
            .order_by(LiberclawAgentVerification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if paid is not None:
        return LiberclawAgentVerificationStatusResponse(
            verified=True,
            status=paid.status,
            order_id=paid.provider_order_id,
            paid_at=paid.paid_at,
        )

    latest = (
        await db.execute(
            select(LiberclawAgentVerification)
            .where(LiberclawAgentVerification.liberclaw_account_id == liberclaw_account_id)
            .order_by(LiberclawAgentVerification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return LiberclawAgentVerificationStatusResponse(verified=False)
    return LiberclawAgentVerificationStatusResponse(
        # Not a constant: READ COMMITTED gives each statement a fresh snapshot, so a
        # webhook can commit a paid row between the two queries.
        verified=latest.status == "paid",
        status=latest.status,
        order_id=latest.provider_order_id,
        paid_at=latest.paid_at,
    )
