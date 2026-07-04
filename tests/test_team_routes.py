"""Team HTTP surface: staff, team-admin and member endpoints + IDOR guards.

Follows ``tests/test_payment_routes.py``: real JWTs via ``create_access_token``,
committed seed sessions (routes open their own ``AsyncSessionLocal``), and per-test
cleanup (the ``async_client`` fixture does no rollback).
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from src.config import config
from src.interfaces.credits import CreditTransactionProvider
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.plan_subscription import PlanSubscription
from src.models.team import Team
from src.models.team_credit_transaction import TeamCreditTransaction
from src.models.team_invite import TeamInvite
from src.models.team_ledger_entry import TeamLedgerEntry
from src.models.team_membership import ROLE_ADMIN, ROLE_MEMBER, TeamMembership
from src.models.user import User
from src.services.auth_tokens import create_access_token
from src.services.payments.base import CheckoutResult
from src.services.payments.registry import payment_registry
from src.services.payments.team_seat_subscription import TEAM_CREDITS_PROVIDER
from src.services.team_credit import TeamCreditService
from src.services.teams import TeamService

ADMIN_TOKEN = "test-admin-token"


def _staff_headers(monkeypatch) -> dict:
    monkeypatch.setattr(config, "ADMIN_SECRET", ADMIN_TOKEN)
    return {"x-admin-token": ADMIN_TOKEN}


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


async def _make_user(email: str | None = None, verified: bool = True) -> User:
    async with AsyncSessionLocal() as db:
        user = User(email=email or f"team-{uuid.uuid4().hex}@example.com", email_verified=verified)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user


async def _seed_team(seat_prices=None, **caps) -> tuple[Team, User]:
    """Committed team with one admin. Returns (team, admin_user)."""
    async with AsyncSessionLocal() as db:
        team = await TeamService.create_team(db, "Acme", seat_prices=seat_prices or {"plus": 16.0}, **caps)
        admin = User(email=f"admin-{uuid.uuid4().hex}@example.com", email_verified=True)
        db.add(admin)
        await db.flush()
        db.add(TeamMembership(team_id=team.id, user_id=admin.id, role=ROLE_ADMIN))
        await db.commit()
        await db.refresh(team)
        await db.refresh(admin)
    return team, admin


async def _add_member(team_id: uuid.UUID, role: str = ROLE_MEMBER) -> User:
    async with AsyncSessionLocal() as db:
        user = User(email=f"member-{uuid.uuid4().hex}@example.com", email_verified=True)
        db.add(user)
        await db.flush()
        db.add(TeamMembership(team_id=team_id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
    return user


async def _topup_team(team_id: uuid.UUID, amount: float) -> None:
    async with AsyncSessionLocal() as db:
        await TeamCreditService.add_credits(db, team_id, amount, CreditTransactionProvider.revolut)
        await db.commit()


async def _cleanup(team_id: uuid.UUID, *user_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(TeamLedgerEntry).where(TeamLedgerEntry.team_id == team_id))
        await db.execute(delete(TeamCreditTransaction).where(TeamCreditTransaction.team_id == team_id))
        await db.execute(delete(TeamInvite).where(TeamInvite.team_id == team_id))
        await db.execute(delete(PlanSubscription).where(PlanSubscription.team_id == team_id))
        await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_id))
        for uid in user_ids:
            await db.execute(delete(PlanSubscription).where(PlanSubscription.user_id == uid))
            await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == uid))
            await db.execute(delete(TeamMembership).where(TeamMembership.user_id == uid))
            await db.execute(delete(User).where(User.id == uid))
        await db.execute(delete(Team).where(Team.id == team_id))
        await db.commit()


def _enable_revolut(monkeypatch, order_id: str):
    revolut = payment_registry.get("revolut")
    monkeypatch.setattr(revolut, "secret_key", "sk_test")
    monkeypatch.setattr(revolut, "webhook_secret", "wsk_test")
    monkeypatch.setattr(config, "FRONTEND_URL", "https://console.libertai.io")

    async def fake_create_topup(
        *, amount, currency, redirect_url, user_email=None, metadata=None, vat_rate=0.0, item_name="Credits"
    ):
        return CheckoutResult(checkout_url="http://pay/team-checkout", order_id=order_id)

    monkeypatch.setattr(revolut, "create_topup", fake_create_topup)


# ---------------------------------------------------------------- Staff endpoints


async def test_staff_create_team_requires_admin_token(async_client, monkeypatch):
    headers = _staff_headers(monkeypatch)
    # Missing token -> rejected (FastAPI required-header validation or 401).
    resp = await async_client.post("/teams/admin", json={"name": "Acme"})
    assert resp.status_code in (401, 422)
    # Wrong token -> 401.
    resp = await async_client.post("/teams/admin", json={"name": "Acme"}, headers={"x-admin-token": "nope"})
    assert resp.status_code == 401
    # Valid token -> 200 + TeamResponse shape.
    resp = await async_client.post(
        "/teams/admin",
        json={"name": "Acme", "seat_prices": {"plus": 16.0}, "extra_credits_monthly_cap": 100.0},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Acme"
    assert body["status"] == "active"
    assert body["seat_prices"] == {"plus": 16.0}
    assert body["extra_credits_monthly_cap"] == 100.0
    team_id = uuid.UUID(body["id"])
    await _cleanup(team_id)


async def test_staff_create_team_rejects_bad_seat_price(async_client, monkeypatch):
    headers = _staff_headers(monkeypatch)
    resp = await async_client.post(
        "/teams/admin", json={"name": "Bad", "seat_prices": {"free": 5.0}}, headers=headers
    )
    assert resp.status_code == 400, resp.text
    assert "tier" in resp.json()["detail"].lower()


async def test_staff_update_prices_and_suspend(async_client, monkeypatch):
    headers = _staff_headers(monkeypatch)
    team, admin = await _seed_team()
    member = await _add_member(team.id)
    # Give the member an active seat so suspension has something to expire.
    async with AsyncSessionLocal() as db:
        db.add(
            PlanSubscription(
                user_id=member.id, tier="plus", provider=TEAM_CREDITS_PROVIDER, status="active",
                team_id=team.id, seat_price_snapshot=16.0,
                current_period_start=datetime.now(), current_period_end=datetime.now() + timedelta(days=15),
            )
        )
        await db.commit()
    try:
        # PATCH prices.
        resp = await async_client.patch(
            f"/teams/admin/{team.id}", json={"seat_prices": {"plus": 20.0, "max": 90.0}}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["seat_prices"] == {"plus": 20.0, "max": 90.0}

        # Unknown tier in prices -> 400.
        resp = await async_client.patch(
            f"/teams/admin/{team.id}", json={"seat_prices": {"nope": 5.0}}, headers=headers
        )
        assert resp.status_code == 400, resp.text

        # Suspend -> team suspended AND the seat expired.
        resp = await async_client.post(f"/teams/admin/{team.id}/suspend", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "suspended"
        async with AsyncSessionLocal() as db:
            seat = (
                await db.execute(select(PlanSubscription).where(PlanSubscription.user_id == member.id))
            ).scalar_one()
            assert seat.status == "expired"
    finally:
        await _cleanup(team.id, admin.id, member.id)


async def test_staff_update_missing_team_404(async_client, monkeypatch):
    headers = _staff_headers(monkeypatch)
    resp = await async_client.patch(f"/teams/admin/{uuid.uuid4()}", json={"name": "X"}, headers=headers)
    assert resp.status_code == 404


async def test_staff_first_invite(async_client, monkeypatch):
    headers = _staff_headers(monkeypatch)
    team, admin = await _seed_team()
    try:
        resp = await async_client.post(
            f"/teams/admin/{team.id}/invites",
            json={"email": "First.Admin@Example.com", "role": "admin"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["email"] == "first.admin@example.com"
        assert body["role"] == "admin"
        assert body["status"] == "pending"
        assert body["expires_at"] is not None
        # An invite row was persisted.
        async with AsyncSessionLocal() as db:
            invite = (
                await db.execute(select(TeamInvite).where(TeamInvite.team_id == team.id))
            ).scalar_one()
            assert invite.email == "first.admin@example.com"
    finally:
        await _cleanup(team.id, admin.id)


async def test_staff_remove_member(async_client, monkeypatch):
    headers = _staff_headers(monkeypatch)
    team, admin = await _seed_team()
    member = await _add_member(team.id)
    try:
        # Staff can remove a regular member.
        resp = await async_client.delete(f"/teams/admin/{team.id}/members/{member.id}", headers=headers)
        assert resp.status_code == 200, resp.text
        async with AsyncSessionLocal() as db:
            gone = (
                await db.execute(select(TeamMembership).where(TeamMembership.user_id == member.id))
            ).scalar_one_or_none()
            assert gone is None
        # Staff cannot remove the last admin.
        resp = await async_client.delete(f"/teams/admin/{team.id}/members/{admin.id}", headers=headers)
        assert resp.status_code == 400, resp.text
        assert "admin" in resp.json()["detail"].lower()
    finally:
        await _cleanup(team.id, admin.id, member.id)


# ---------------------------------------------------------------- Team admin endpoints


async def test_admin_invite_accept_flow(async_client, monkeypatch):
    team, admin = await _seed_team()
    invitee = await _make_user()
    try:
        # Admin creates an invite via the route (email send is mocked/logged).
        resp = await async_client.post(
            f"/teams/{team.id}/invites",
            json={"email": invitee.email, "role": "member"},
            headers=_auth(admin.id),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] == invitee.email.lower()
        assert resp.json()["status"] == "pending"

        # Mint a usable token for the same invitee (the route hides the plaintext token).
        async with AsyncSessionLocal() as db:
            _, token = await TeamService.create_invite(db, team.id, invitee.email, "member", "staff")
            await db.commit()

        # Invitee accepts.
        resp = await async_client.post(
            "/teams/invites/accept", json={"token": token}, headers=_auth(invitee.id)
        )
        assert resp.status_code == 200, resp.text

        # Admin's /me now lists the invitee among members.
        resp = await async_client.get("/teams/me", headers=_auth(admin.id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert str(invitee.id) in {m["user_id"] for m in body["members"]}
    finally:
        await _cleanup(team.id, admin.id, invitee.id)


async def test_admin_accept_invite_wrong_email_rejected(async_client, monkeypatch):
    team, admin = await _seed_team()
    other = await _make_user()
    try:
        async with AsyncSessionLocal() as db:
            _, token = await TeamService.create_invite(db, team.id, "someone-else@example.com", "member", "staff")
            await db.commit()
        resp = await async_client.post(
            "/teams/invites/accept", json={"token": token}, headers=_auth(other.id)
        )
        assert resp.status_code == 400, resp.text
        assert "email" in resp.json()["detail"].lower()
    finally:
        await _cleanup(team.id, admin.id, other.id)


async def test_admin_assign_seat_and_ledger(async_client, monkeypatch):
    team, admin = await _seed_team()
    member = await _add_member(team.id)
    await _topup_team(team.id, 100.0)
    try:
        # Assign a seat -> prorated charge against the balance.
        resp = await async_client.post(
            f"/teams/{team.id}/seats",
            json={"user_id": str(member.id), "tier": "plus"},
            headers=_auth(admin.id),
        )
        assert resp.status_code == 200, resp.text

        # Ledger shows the seat charge + a topup + reduced balance.
        resp = await async_client.get(f"/teams/{team.id}/ledger", headers=_auth(admin.id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["balance"] < 100.0
        assert any(c["entry_type"] == "seat_charge_prorated" for c in body["charges"])
        assert len(body["topups"]) >= 1
        assert body["topups"][0]["amount"] == 100.0
    finally:
        await _cleanup(team.id, admin.id, member.id)


async def test_admin_caps_endpoints(async_client, monkeypatch):
    team, admin = await _seed_team()
    member = await _add_member(team.id)
    try:
        # Team caps.
        resp = await async_client.patch(
            f"/teams/{team.id}/caps",
            json={"extra_credits_monthly_cap": 200.0, "extra_credits_member_default_cap": 20.0},
            headers=_auth(admin.id),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["extra_credits_monthly_cap"] == 200.0
        assert resp.json()["extra_credits_member_default_cap"] == 20.0

        # Per-member override.
        resp = await async_client.patch(
            f"/teams/{team.id}/members/{member.id}/cap",
            json={"extra_credits_cap_override": 5.0},
            headers=_auth(admin.id),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["extra_credits_cap_override"] == 5.0
        async with AsyncSessionLocal() as db:
            m = (
                await db.execute(select(TeamMembership).where(TeamMembership.user_id == member.id))
            ).scalar_one()
            assert m.extra_credits_cap_override == 5.0
    finally:
        await _cleanup(team.id, admin.id, member.id)


async def test_admin_usage_view(async_client, monkeypatch):
    team, admin = await _seed_team(extra_credits_monthly_cap=100.0, extra_credits_member_default_cap=50.0)
    member = await _add_member(team.id)
    try:
        resp = await async_client.get(f"/teams/{team.id}/usage", headers=_auth(admin.id))
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        by_user = {r["user_id"]: r for r in rows}
        assert str(member.id) in by_user
        row = by_user[str(member.id)]
        assert row["window_5h_used"] == 0.0
        assert row["weekly_used"] == 0.0
        assert row["extra_credits_month_to_date"] == 0.0
    finally:
        await _cleanup(team.id, admin.id, member.id)


async def test_admin_topup_returns_checkout_url(async_client, monkeypatch):
    team, admin = await _seed_team()
    member = await _add_member(team.id)
    _enable_revolut(monkeypatch, order_id=f"team_ord_{team.id}")
    try:
        # Non-admin member cannot open a team top-up.
        resp = await async_client.post(
            f"/teams/{team.id}/topup", json={"amount": 50.0}, headers=_auth(member.id)
        )
        assert resp.status_code == 403, resp.text

        # Admin gets a checkout URL + a pending team credit transaction.
        resp = await async_client.post(
            f"/teams/{team.id}/topup", json={"amount": 50.0}, headers=_auth(admin.id)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["checkout_url"] == "http://pay/team-checkout"
        async with AsyncSessionLocal() as db:
            tx = (
                await db.execute(
                    select(TeamCreditTransaction).where(TeamCreditTransaction.team_id == team.id)
                )
            ).scalar_one()
            assert float(tx.amount) == 50.0
    finally:
        await _cleanup(team.id, admin.id, member.id)


# ---------------------------------------------------------------- Member endpoints


async def test_member_me_hides_balance_and_members(async_client, monkeypatch):
    team, admin = await _seed_team()
    member = await _add_member(team.id)
    try:
        resp = await async_client.get("/teams/me", headers=_auth(member.id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["role"] == "member"
        assert body["team"]["id"] == str(team.id)
        assert body["balance"] is None
        assert body["members"] is None

        # Admin sees both.
        resp = await async_client.get("/teams/me", headers=_auth(admin.id))
        body = resp.json()
        assert body["role"] == "admin"
        assert body["balance"] is not None
        assert body["members"] is not None
    finally:
        await _cleanup(team.id, admin.id, member.id)


async def test_member_me_404_when_no_team(async_client, monkeypatch):
    user = await _make_user()
    try:
        resp = await async_client.get("/teams/me", headers=_auth(user.id))
        assert resp.status_code == 404
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()


async def test_member_cannot_admin_endpoints(async_client, monkeypatch):
    team, admin = await _seed_team()
    member = await _add_member(team.id)
    _enable_revolut(monkeypatch, order_id=f"team_ord_forbidden_{team.id}")
    h = _auth(member.id)
    try:
        assert (
            await async_client.post(
                f"/teams/{team.id}/invites", json={"email": "x@example.com"}, headers=h
            )
        ).status_code == 403
        assert (
            await async_client.post(
                f"/teams/{team.id}/seats", json={"user_id": str(member.id), "tier": "plus"}, headers=h
            )
        ).status_code == 403
        assert (await async_client.get(f"/teams/{team.id}/ledger", headers=h)).status_code == 403
        assert (await async_client.get(f"/teams/{team.id}/usage", headers=h)).status_code == 403
        assert (
            await async_client.patch(
                f"/teams/{team.id}/caps", json={"extra_credits_monthly_cap": 10.0}, headers=h
            )
        ).status_code == 403
        assert (
            await async_client.post(f"/teams/{team.id}/topup", json={"amount": 5.0}, headers=h)
        ).status_code == 403
    finally:
        await _cleanup(team.id, admin.id, member.id)


async def test_leave_team(async_client, monkeypatch):
    team, admin = await _seed_team()
    member = await _add_member(team.id)
    try:
        resp = await async_client.post("/teams/leave", headers=_auth(member.id))
        assert resp.status_code == 200, resp.text
        async with AsyncSessionLocal() as db:
            gone = (
                await db.execute(select(TeamMembership).where(TeamMembership.user_id == member.id))
            ).scalar_one_or_none()
            assert gone is None

        # The last admin cannot leave.
        resp = await async_client.post("/teams/leave", headers=_auth(admin.id))
        assert resp.status_code == 400, resp.text
        assert "admin" in resp.json()["detail"].lower()
    finally:
        await _cleanup(team.id, admin.id, member.id)


# ---------------------------------------------------------------- IDOR


async def test_team_b_admin_cannot_touch_team_a(async_client, monkeypatch):
    team_a, admin_a = await _seed_team()
    member_a = await _add_member(team_a.id)
    team_b, admin_b = await _seed_team()
    _enable_revolut(monkeypatch, order_id=f"idor_{team_a.id}")
    hb = _auth(admin_b.id)  # admin of team B acting on team A
    try:
        assert (
            await async_client.post(
                f"/teams/{team_a.id}/invites", json={"email": "x@example.com"}, headers=hb
            )
        ).status_code == 403
        assert (
            await async_client.post(
                f"/teams/{team_a.id}/seats", json={"user_id": str(member_a.id), "tier": "plus"}, headers=hb
            )
        ).status_code == 403
        assert (
            await async_client.patch(
                f"/teams/{team_a.id}/seats/{member_a.id}", json={"tier": "max"}, headers=hb
            )
        ).status_code == 403
        assert (
            await async_client.delete(f"/teams/{team_a.id}/seats/{member_a.id}", headers=hb)
        ).status_code == 403
        assert (
            await async_client.delete(f"/teams/{team_a.id}/members/{member_a.id}", headers=hb)
        ).status_code == 403
        assert (
            await async_client.post(
                f"/teams/{team_a.id}/members/{member_a.id}/role", json={"role": "admin"}, headers=hb
            )
        ).status_code == 403
        assert (await async_client.get(f"/teams/{team_a.id}/ledger", headers=hb)).status_code == 403
        assert (await async_client.get(f"/teams/{team_a.id}/usage", headers=hb)).status_code == 403
        assert (
            await async_client.patch(
                f"/teams/{team_a.id}/caps", json={"extra_credits_monthly_cap": 1.0}, headers=hb
            )
        ).status_code == 403
        assert (
            await async_client.patch(
                f"/teams/{team_a.id}/members/{member_a.id}/cap",
                json={"extra_credits_cap_override": 1.0},
                headers=hb,
            )
        ).status_code == 403
        assert (
            await async_client.post(f"/teams/{team_a.id}/topup", json={"amount": 5.0}, headers=hb)
        ).status_code == 403
        assert (
            await async_client.delete(f"/teams/{team_a.id}/invites/{uuid.uuid4()}", headers=hb)
        ).status_code == 403
    finally:
        await _cleanup(team_a.id, admin_a.id, member_a.id)
        await _cleanup(team_b.id, admin_b.id)
