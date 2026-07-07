import uuid

import pytest
from sqlalchemy import select

from src.interfaces.credits import CreditTransactionStatus
from src.models.team_credit_transaction import TeamCreditTransaction
from src.services.payments.base import CheckoutResult, PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager
from src.services.team_credit import TeamCreditService
from src.services.teams import TeamService


class _FakeProvider:
    id = "revolut"

    def supports(self, capability):
        return True

    async def create_topup(self, **kwargs):
        self.last_topup = kwargs
        return CheckoutResult(checkout_url="https://pay.example/x", order_id="order-123")


@pytest.mark.asyncio
async def test_start_team_topup_creates_pending_team_row(db):
    team = await TeamService.create_team(db, "Acme")
    provider = _FakeProvider()
    manager = PaymentManager(provider, db)

    result = await manager.start_team_topup(
        team, admin_email="admin@corp.dev", redirect_url="https://app/cb", usd_credits=200.0
    )

    assert result.order_id == "order-123"
    tx = (
        await db.execute(
            select(TeamCreditTransaction).where(TeamCreditTransaction.team_id == team.id)
        )
    ).scalar_one()
    assert tx.status == CreditTransactionStatus.pending
    assert tx.amount == 200.0
    assert tx.external_reference == "revolut:order-123"
    assert provider.last_topup["metadata"]["ext_ref"] == f"topup:team:{team.id}"
    # Pending rows don't count toward the balance.
    assert await TeamCreditService.get_balance(db, team.id) == 0.0


@pytest.mark.asyncio
async def test_settle_completes_team_topup(db):
    team = await TeamService.create_team(db, "Acme")
    provider = _FakeProvider()
    manager = PaymentManager(provider, db)
    await manager.start_team_topup(team, admin_email=None, redirect_url="https://app/cb", usd_credits=150.0)

    handled = await manager._settle_topup(
        PaymentEvent(
            provider="revolut",
            type=PaymentEventType.order_completed,
            provider_event_id=f"ORDER_COMPLETED:order-123:{uuid.uuid4()}",
            order_id="order-123",
        )
    )
    assert handled is True
    assert await TeamCreditService.get_balance(db, team.id) == 150.0
    # Replay is idempotent.
    await manager._settle_topup(
        PaymentEvent(
            provider="revolut",
            type=PaymentEventType.order_completed,
            provider_event_id=f"ORDER_COMPLETED:order-123:{uuid.uuid4()}",
            order_id="order-123",
        )
    )
    assert await TeamCreditService.get_balance(db, team.id) == 150.0


@pytest.mark.asyncio
async def test_settle_fails_team_topup_on_failed_order(db):
    team = await TeamService.create_team(db, "Acme")
    manager = PaymentManager(_FakeProvider(), db)
    await manager.start_team_topup(team, admin_email=None, redirect_url="https://app/cb", usd_credits=150.0)

    await manager._settle_topup(
        PaymentEvent(
            provider="revolut",
            type=PaymentEventType.order_failed,
            provider_event_id=f"ORDER_FAILED:order-123:{uuid.uuid4()}",
            order_id="order-123",
        )
    )
    tx = (
        await db.execute(
            select(TeamCreditTransaction).where(TeamCreditTransaction.team_id == team.id)
        )
    ).scalar_one()
    assert tx.status == CreditTransactionStatus.error and tx.is_active is False and tx.amount_left == 0


@pytest.mark.asyncio
async def test_out_of_order_failed_does_not_zero_completed_team_topup(db):
    """An order_failed arriving after completion must not erase a spendable balance."""
    team = await TeamService.create_team(db, "Acme")
    manager = PaymentManager(_FakeProvider(), db)
    await manager.start_team_topup(team, admin_email=None, redirect_url="https://app/cb", usd_credits=150.0)

    # Complete first.
    await manager._settle_topup(
        PaymentEvent(
            provider="revolut",
            type=PaymentEventType.order_completed,
            provider_event_id=f"ORDER_COMPLETED:order-123:{uuid.uuid4()}",
            order_id="order-123",
        )
    )
    assert await TeamCreditService.get_balance(db, team.id) == 150.0

    # A late failed event must be ignored — balance and status unchanged.
    await manager._settle_topup(
        PaymentEvent(
            provider="revolut",
            type=PaymentEventType.order_failed,
            provider_event_id=f"ORDER_FAILED:order-123:{uuid.uuid4()}",
            order_id="order-123",
        )
    )
    tx = (
        await db.execute(
            select(TeamCreditTransaction).where(TeamCreditTransaction.team_id == team.id)
        )
    ).scalar_one()
    assert tx.status == CreditTransactionStatus.completed and tx.is_active is True
    assert await TeamCreditService.get_balance(db, team.id) == 150.0
