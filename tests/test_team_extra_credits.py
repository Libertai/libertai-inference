"""Team extra-credits gating: entitlement context, chokepoint drain, personal blocks.

The pure/entitlement tests use the rolled-back ``db`` fixture. The chokepoint and
route tests exercise the real services (which open their own ``AsyncSessionLocal``),
so they seed via committed sessions and clean up their own rows — mirroring
``tests/test_inference_call_billing.py`` and ``tests/test_payment_routes.py``.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from src.interfaces.api_keys import ApiKeyType
from src.interfaces.credits import CreditTransactionProvider, CreditTransactionStatus
from src.models.api_key import ApiKey as ApiKeyDB
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.inference_call import InferenceCall
from src.models.plan_subscription import PlanSubscription
from src.models.team import Team
from src.models.team_credit_transaction import TeamCreditTransaction
from src.models.team_ledger_entry import TeamLedgerEntry
from src.models.team_membership import ROLE_MEMBER, TeamMembership
from src.models.user import User
from src.services.api_key import ApiKeyService
from src.services.auth_tokens import create_access_token
from src.services.entitlement import compute_source, get_team_extra_context
from src.services.payments.registry import payment_registry
from src.services.payments.team_seat_subscription import TEAM_CREDITS_PROVIDER
from src.services.team_credit import TeamCreditService
from src.services.teams import TeamService
from src.subscription_tiers import get_tier


async def _member_with_seat(db, member_cap=None, team_cap=None, balance=100.0):
    team = await TeamService.create_team(
        db, "Acme", seat_prices={"plus": 16.0},
        extra_credits_monthly_cap=team_cap, extra_credits_member_default_cap=member_cap,
    )
    user = User(email=f"{uuid.uuid4()}@test.dev")
    db.add(user)
    await db.flush()
    db.add(TeamMembership(team_id=team.id, user_id=user.id, role=ROLE_MEMBER))
    seat = PlanSubscription(
        user_id=user.id, tier="plus", provider=TEAM_CREDITS_PROVIDER, status="active",
        team_id=team.id, seat_price_snapshot=16.0,
        current_period_start=datetime.now(), current_period_end=datetime.now() + timedelta(days=20),
    )
    db.add(seat)
    await db.flush()
    if balance:
        await TeamCreditService.add_credits(db, team.id, balance, CreditTransactionProvider.revolut)
    return team, user


def test_compute_source_team_path():
    tier = get_tier("free")  # tiny windows: 0.5 / 2.0
    # Windows exhausted, no prepaid, team available -> "team"
    assert compute_source(tier, 0.5, 2.0, 0.0, team_extra_available=5.0) == "team"
    # Windows exhausted, nothing anywhere -> blocked
    assert compute_source(tier, 0.5, 2.0, 0.0, team_extra_available=0.0) == "blocked"
    # Windows open -> tier, regardless of team
    assert compute_source(tier, 0.0, 0.0, 0.0, team_extra_available=5.0) == "tier"


@pytest.mark.asyncio
async def test_context_caps_default_to_zero(db):
    _, user = await _member_with_seat(db, member_cap=None, team_cap=None)
    ctx = await get_team_extra_context(db, user.id, datetime.now())
    assert ctx is not None and ctx.available == 0.0  # caps unset -> blocked


@pytest.mark.asyncio
async def test_context_min_of_caps_and_balance(db):
    team, user = await _member_with_seat(db, member_cap=10.0, team_cap=50.0, balance=7.0)
    ctx = await get_team_extra_context(db, user.id, datetime.now())
    assert ctx.available == 7.0  # balance is the binding constraint

    await TeamCreditService.add_credits(db, team.id, 100.0, CreditTransactionProvider.revolut)
    ctx = await get_team_extra_context(db, user.id, datetime.now())
    assert ctx.available == 10.0  # member cap binds now


@pytest.mark.asyncio
async def test_context_subtracts_month_to_date_team_spend(db):
    team, user = await _member_with_seat(db, member_cap=50.0, team_cap=20.0, balance=100.0)
    key = ApiKeyDB(key=f"k-{uuid.uuid4()}", user_id=user.id, name="k", type=ApiKeyType.api)
    db.add(key)
    await db.flush()
    # 12 total, 4 window-covered -> 8 team-funded this month.
    call = InferenceCall(api_key_id=key.id, credits_used=12.0, model_name="m",
                         tier_credits_used=4.0, team_id=team.id)
    call.used_at = datetime.now()
    db.add(call)
    await db.flush()
    ctx = await get_team_extra_context(db, user.id, datetime.now())
    assert ctx.available == 12.0  # team cap 20 - 8 spent


@pytest.mark.asyncio
async def test_context_none_for_non_member_and_zero_for_suspended(db):
    outsider = User(email=f"{uuid.uuid4()}@test.dev")
    db.add(outsider)
    await db.flush()
    assert await get_team_extra_context(db, outsider.id, datetime.now()) is None

    team, user = await _member_with_seat(db, member_cap=10.0, team_cap=50.0)
    team.status = "suspended"
    ctx = await get_team_extra_context(db, user.id, datetime.now())
    assert ctx.available == 0.0


@pytest.mark.asyncio
async def test_member_override_beats_team_default(db):
    team, user = await _member_with_seat(db, member_cap=10.0, team_cap=50.0)
    membership = await TeamService.get_membership(db, user.id)
    membership.extra_credits_cap_override = 3.0
    await db.flush()
    ctx = await get_team_extra_context(db, user.id, datetime.now())
    assert ctx.available == 3.0


# --- chokepoint (committed sessions; register_inference_call opens its own) ---


async def _committed_member(*, member_cap, team_cap, balance, personal_prepaid=0.0):
    """Seed a committed team + member + api key. Returns (team_id, user_id, key_str)."""
    async with AsyncSessionLocal() as db:
        team = await TeamService.create_team(
            db, "Acme", seat_prices={"plus": 16.0},
            extra_credits_monthly_cap=team_cap, extra_credits_member_default_cap=member_cap,
        )
        user = User(email=f"{uuid.uuid4()}@test.dev", email_verified=True)
        db.add(user)
        await db.flush()
        db.add(TeamMembership(team_id=team.id, user_id=user.id, role=ROLE_MEMBER))
        key = ApiKeyDB(key=f"k-{uuid.uuid4()}", user_id=user.id, name="k", type=ApiKeyType.api)
        db.add(key)
        if personal_prepaid:
            db.add(CreditTransaction(
                user_id=user.id, amount=personal_prepaid, amount_left=personal_prepaid,
                provider=CreditTransactionProvider.revolut, status=CreditTransactionStatus.completed,
            ))
        await db.flush()
        if balance:
            await TeamCreditService.add_credits(db, team.id, balance, CreditTransactionProvider.revolut)
        await db.commit()
        return team.id, user.id, key.key


async def _cleanup_member(team_id, user_id):
    async with AsyncSessionLocal() as db:
        from src.models.entitlement_window import EntitlementWindow

        await db.execute(delete(TeamLedgerEntry).where(TeamLedgerEntry.team_id == team_id))
        await db.execute(delete(TeamCreditTransaction).where(TeamCreditTransaction.team_id == team_id))
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(EntitlementWindow).where(EntitlementWindow.user_id == user_id))
        await db.execute(delete(PlanSubscription).where(PlanSubscription.user_id == user_id))
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == user_id))
        # inference_calls cascade via api_keys FK; delete keys then user, then team.
        await db.execute(delete(ApiKeyDB).where(ApiKeyDB.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Team).where(Team.id == team_id))
        await db.commit()


async def test_register_inference_call_drains_team_and_stamps():
    # Free-tier windows (5h=0.5, weekly=2.0); a 3.0 call is 0.5 tier-covered, 2.5 overflow.
    team_id, user_id, key = await _committed_member(
        member_cap=100.0, team_cap=100.0, balance=100.0, personal_prepaid=5.0
    )
    try:
        ok = await ApiKeyService.register_inference_call(key, credits_used=3.0, model_name="m")
        assert ok is True
        async with AsyncSessionLocal() as s:
            call = (
                await s.execute(
                    select(InferenceCall)
                    .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
                    .where(ApiKeyDB.user_id == user_id)
                )
            ).scalar_one()
            # (1) the row is stamped with the funding team.
            assert call.team_id == team_id
            assert call.tier_credits_used == pytest.approx(0.5)
            # (2) team balance dropped by (credits_used - tier_credits_used) = 2.5.
            assert await TeamCreditService.get_balance(s, team_id) == pytest.approx(97.5)
            # (3) an extra_credits_usage ledger entry for that amount exists.
            entry = (
                await s.execute(
                    select(TeamLedgerEntry).where(
                        TeamLedgerEntry.team_id == team_id,
                        TeamLedgerEntry.entry_type == "extra_credits_usage",
                    )
                )
            ).scalar_one()
            assert entry.amount == pytest.approx(2.5)
            # (4) the member's PERSONAL prepaid is untouched.
            personal = (
                await s.execute(select(CreditTransaction).where(CreditTransaction.user_id == user_id))
            ).scalar_one()
            assert personal.amount_left == pytest.approx(5.0)
    finally:
        await _cleanup_member(team_id, user_id)


async def test_register_inference_call_blocked_caps_leaves_team_balance():
    # Caps unset (None -> 0): the member has a team but zero headroom, so the overflow
    # is NOT drained, team_id stays NULL, and the balance is left intact.
    team_id, user_id, key = await _committed_member(
        member_cap=None, team_cap=None, balance=100.0, personal_prepaid=5.0
    )
    try:
        ok = await ApiKeyService.register_inference_call(key, credits_used=3.0, model_name="m")
        assert ok is True
        async with AsyncSessionLocal() as s:
            call = (
                await s.execute(
                    select(InferenceCall)
                    .join(ApiKeyDB, InferenceCall.api_key_id == ApiKeyDB.id)
                    .where(ApiKeyDB.user_id == user_id)
                )
            ).scalar_one()
            assert call.team_id is None  # not funded by the team
            # Team balance untouched (nothing drained).
            assert await TeamCreditService.get_balance(s, team_id) == pytest.approx(100.0)
            # No extra_credits_usage ledger entry written.
            entries = (
                await s.execute(
                    select(TeamLedgerEntry).where(
                        TeamLedgerEntry.team_id == team_id,
                        TeamLedgerEntry.entry_type == "extra_credits_usage",
                    )
                )
            ).scalars().all()
            assert entries == []
            # Personal prepaid untouched (members never spend it).
            personal = (
                await s.execute(select(CreditTransaction).where(CreditTransaction.user_id == user_id))
            ).scalar_one()
            assert personal.amount_left == pytest.approx(5.0)
    finally:
        await _cleanup_member(team_id, user_id)


# --- route guards: members can't hold personal credits ---


async def _committed_member_only():
    """A committed email member with no seat. Returns (team_id, user_id, email)."""
    async with AsyncSessionLocal() as db:
        team = await TeamService.create_team(db, "Acme", seat_prices={"plus": 16.0})
        email = f"{uuid.uuid4()}@test.dev"
        user = User(email=email, email_verified=True)
        db.add(user)
        await db.flush()
        db.add(TeamMembership(team_id=team.id, user_id=user.id, role=ROLE_MEMBER))
        await db.commit()
        return team.id, user.id, email


async def _cleanup_member_only(team_id, user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
        await db.execute(delete(TeamMembership).where(TeamMembership.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Team).where(Team.id == team_id))
        await db.commit()


async def test_personal_topup_and_voucher_blocked_for_members(async_client, monkeypatch):
    from src.config import config

    monkeypatch.setattr("src.routes.payments.payments.resolve_currency", lambda request: "USD")
    monkeypatch.setattr(config, "VOUCHERS_PASSWORDS", ["vpw"])
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")

    team_id, user_id, email = await _committed_member_only()
    headers = {"Authorization": f"Bearer {create_access_token(user_id)}"}
    try:
        # Personal top-up: blocked because the caller is a team member.
        resp = await async_client.post(
            "/payments/topup", json={"provider": "revolut", "amount": 10}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert "team" in resp.json()["detail"].lower()

        # Voucher to a member's email: blocked for the same reason.
        resp = await async_client.post(
            "/credits/vouchers", json={"email": email, "amount": 5, "password": "vpw"}
        )
        assert resp.status_code == 400, resp.text
        assert "team" in resp.json()["detail"].lower()
    finally:
        await _cleanup_member_only(team_id, user_id)
