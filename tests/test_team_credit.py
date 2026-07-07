import pytest

from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.team import Team
from src.services.team_credit import TeamCreditService


async def _team(db) -> Team:
    team = Team(name="Acme")
    db.add(team)
    await db.flush()
    return team


@pytest.mark.asyncio
async def test_balance_sums_only_completed_active(db):
    team = await _team(db)
    await TeamCreditService.add_credits(db, team.id, 100.0, CreditTransactionProvider.revolut)
    await TeamCreditService.add_credits(
        db, team.id, 50.0, CreditTransactionProvider.revolut,
        external_reference="pending-1", status=CreditTransactionStatus.pending,
    )
    assert await TeamCreditService.get_balance(db, team.id) == 100.0


@pytest.mark.asyncio
async def test_add_credits_dedups_external_reference(db):
    team = await _team(db)
    first = await TeamCreditService.add_credits(
        db, team.id, 10.0, CreditTransactionProvider.revolut, external_reference="revolut:o1"
    )
    dup = await TeamCreditService.add_credits(
        db, team.id, 10.0, CreditTransactionProvider.revolut, external_reference="revolut:o1"
    )
    assert first is not None and dup is None
    assert await TeamCreditService.get_balance(db, team.id) == 10.0


@pytest.mark.asyncio
async def test_use_credits_drains_oldest_first(db):
    team = await _team(db)
    tx1 = await TeamCreditService.add_credits(db, team.id, 30.0, CreditTransactionProvider.revolut)
    tx2 = await TeamCreditService.add_credits(db, team.id, 30.0, CreditTransactionProvider.revolut)
    assert await TeamCreditService.use_credits(db, team.id, 40.0) is True
    assert tx1.amount_left == 0.0
    assert tx2.amount_left == 20.0


@pytest.mark.asyncio
async def test_use_credits_insufficient_no_partial(db):
    team = await _team(db)
    tx = await TeamCreditService.add_credits(db, team.id, 10.0, CreditTransactionProvider.revolut)
    assert await TeamCreditService.use_credits(db, team.id, 25.0) is False
    assert tx.amount_left == 10.0  # untouched without allow_partial


@pytest.mark.asyncio
async def test_use_credits_allow_partial_drains_to_zero(db):
    team = await _team(db)
    await TeamCreditService.add_credits(db, team.id, 10.0, CreditTransactionProvider.revolut)
    assert await TeamCreditService.use_credits(db, team.id, 25.0, allow_partial=True) is False
    assert await TeamCreditService.get_balance(db, team.id) == 0.0


@pytest.mark.asyncio
async def test_ledger_entry_written(db):
    from sqlalchemy import select

    from src.models.team_ledger_entry import TeamLedgerEntry

    team = await _team(db)
    await TeamCreditService.log(db, team.id, "monthly_renewal", 36.0, {"seats": 2})
    entry = (await db.execute(select(TeamLedgerEntry).where(TeamLedgerEntry.team_id == team.id))).scalar_one()
    assert entry.entry_type == "monthly_renewal" and entry.amount == 36.0
