"""Token-authed /liberclaw subscription + admin endpoints, and the flag-gated pieces they
share with the payments webhook (200-skip) and the retained /liberclaw/invoices path
(foreign-order product check)."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select

from src.config import config
from src.models.base import AsyncSessionLocal
from src.models.liberclaw_user import LiberclawUser
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.routes.liberclaw import liberclaw as liberclaw_routes
from src.services.payments.base import CheckoutResult, PaymentEvent, PaymentEventType
from src.services.payments.manager import PaymentManager
from src.subscription_tiers import PRODUCT_LIBERCLAW
from tests.test_liberclaw_invoice_endpoints import HEADERS, _cleanup, _install_fake_provider, _order, _post
from tests.test_payment_manager import FakeProvider


@pytest.fixture(autouse=True)
def _liberclaw_secret(monkeypatch):
    """HEADERS is imported from test_liberclaw_invoice_endpoints, which keys on this same
    secret — its autouse fixture only applies within that module, so it's repeated here."""
    monkeypatch.setattr(config, "LIBERCLAW_SECRET", HEADERS["x-liberclaw-token"])


@pytest.fixture(autouse=True)
def _billing_enabled(monkeypatch):
    """Every mutating subscription route is refused while the cutover flag is off; the tests
    that assert that refusal flip it back themselves."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)


# --------------------------------------------------------------------- fixtures / helpers


async def _cleanup_account(account_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        sub_ids = (
            (await db.execute(select(PlanSubscription.id).where(PlanSubscription.liberclaw_account_id == account_id)))
            .scalars()
            .all()
        )
        if sub_ids:
            await db.execute(delete(PlanSubscriptionEvent).where(PlanSubscriptionEvent.subscription_id.in_(sub_ids)))
        await db.execute(delete(PlanSubscription).where(PlanSubscription.liberclaw_account_id == account_id))
        await db.execute(delete(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
        await db.commit()


async def _seed_lc_user(account_id: uuid.UUID, tier: str = "free") -> str:
    email = f"{account_id.hex}@example.com"
    async with AsyncSessionLocal() as db:
        db.add(LiberclawUser(user_id=email, user_type="email", tier=tier, liberclaw_account_id=account_id))
        await db.commit()
    return email


async def _seed_sub(account_id: uuid.UUID, *, tier: str = "starter", status: str = "active", **overrides) -> uuid.UUID:
    defaults = {
        "user_id": None,
        "tier": tier,
        "provider": "revolut",
        "status": status,
        "provider_subscription_id": f"psub_{uuid.uuid4().hex}",
        "product": PRODUCT_LIBERCLAW,
        "liberclaw_account_id": account_id,
        "currency": "EUR",
        "current_period_start": datetime.now() - timedelta(days=1),
        "current_period_end": datetime.now() + timedelta(days=29),
    }
    defaults.update(overrides)
    async with AsyncSessionLocal() as db:
        sub = PlanSubscription(**defaults)
        db.add(sub)
        await db.commit()
        await db.refresh(sub)
        return sub.id


async def _get_sub(sub_id: uuid.UUID) -> PlanSubscription:
    async with AsyncSessionLocal() as db:
        return await db.get(PlanSubscription, sub_id)


# --------------------------------------------------------------------- checkout / EUR pinning


async def test_checkout_happy_path_upserts_bridge_and_pins_eur(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    try:
        resp = await async_client.post(
            "/liberclaw/checkout",
            headers=HEADERS,
            json={
                "liberclaw_account_id": str(account_id),
                "email": "buyer@example.com",
                "tier": "starter",
                "redirect_url": "http://lc.example/callback",
                "currency": "USD",  # smuggled — must be ignored, never reach the provider
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["url"]
        sub_id = uuid.UUID(body["subscription_id"])

        assert fake.sub_currencies[-1] == "EUR"
        assert fake.sub_products[-1] == "liberclaw"

        async with AsyncSessionLocal() as db:
            lc_user = (
                await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
            ).scalar_one()
        assert lc_user.user_id == "buyer@example.com"

        sub = await _get_sub(sub_id)
        assert sub.tier == "starter"
        assert sub.status == "pending"
        assert sub.product == PRODUCT_LIBERCLAW
    finally:
        await _cleanup_account(account_id)


async def test_checkout_backfills_legacy_bridge_row_without_account_id(async_client, monkeypatch):
    """Reproduces prod state (a): the dedupe migration can leave a (email, 'email') row with
    liberclaw_account_id NULL. A blind INSERT on checkout would collide on unique_liberclaw_user
    instead of backfilling this row."""
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    email = f"legacy-{uuid.uuid4().hex}@example.com"
    async with AsyncSessionLocal() as db:
        db.add(LiberclawUser(user_id=email, user_type="email", liberclaw_account_id=None))
        await db.commit()
    try:
        resp = await async_client.post(
            "/liberclaw/checkout",
            headers=HEADERS,
            json={
                "liberclaw_account_id": str(account_id),
                "email": email,
                "tier": "starter",
                "redirect_url": "http://lc.example/callback",
            },
        )
        assert resp.status_code == 200

        async with AsyncSessionLocal() as db:
            rows = (
                (
                    await db.execute(
                        select(LiberclawUser).where(LiberclawUser.user_id == email, LiberclawUser.user_type == "email")
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1  # no duplicate row
        assert rows[0].liberclaw_account_id == account_id  # backfilled onto the legacy row
    finally:
        await _cleanup_account(account_id)


async def test_checkout_email_rename_collision_is_skipped_not_raised(async_client, monkeypatch):
    """Reproduces prod state (b): renaming an account's bridge row to an email another
    account's row already holds must log-and-skip via _refresh_email_if_safe, never 500."""
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    old_email = f"old-{uuid.uuid4().hex}@example.com"
    taken_email = f"taken-{uuid.uuid4().hex}@example.com"
    async with AsyncSessionLocal() as db:
        db.add(LiberclawUser(user_id=old_email, user_type="email", liberclaw_account_id=account_id))
        db.add(LiberclawUser(user_id=taken_email, user_type="email", liberclaw_account_id=other_account_id))
        await db.commit()
    try:
        resp = await async_client.post(
            "/liberclaw/checkout",
            headers=HEADERS,
            json={
                "liberclaw_account_id": str(account_id),
                "email": taken_email,
                "tier": "starter",
                "redirect_url": "http://lc.example/callback",
            },
        )
        assert resp.status_code == 200

        async with AsyncSessionLocal() as db:
            own_row = (
                await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
            ).scalar_one()
            other_row = (
                await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == other_account_id))
            ).scalar_one()
        assert own_row.user_id == old_email  # rename skipped: the target email is already taken
        assert other_row.user_id == taken_email  # untouched
    finally:
        await _cleanup_account(account_id)
        await _cleanup_account(other_account_id)


async def test_checkout_email_bound_to_another_account_is_409_before_any_provider_call(async_client, monkeypatch):
    """Unresolvable identity: the account has no bridge row of its own and the email's row
    belongs to someone else. Nothing can bridge it, so every activation webhook would raise —
    the checkout must be refused before the customer is charged."""
    fake = _install_fake_provider(monkeypatch)
    errors: list[str] = []
    monkeypatch.setattr(liberclaw_routes.logger, "error", lambda msg, **kw: errors.append(msg))

    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    email = f"shared-{uuid.uuid4().hex}@example.com"
    async with AsyncSessionLocal() as db:
        db.add(LiberclawUser(user_id=email, user_type="email", liberclaw_account_id=other_account_id))
        await db.commit()
    try:
        resp = await async_client.post(
            "/liberclaw/checkout",
            headers=HEADERS,
            json={
                "liberclaw_account_id": str(account_id),
                "email": email,
                "tier": "starter",
                "redirect_url": "http://lc.example/callback",
            },
        )
        assert resp.status_code == 409

        assert fake.sub_seq == 0  # no Revolut subscription created
        assert fake.sub_products == []
        assert any(str(account_id) in m and str(other_account_id) in m for m in errors)

        async with AsyncSessionLocal() as db:
            subs = (
                (await db.execute(select(PlanSubscription).where(PlanSubscription.liberclaw_account_id == account_id)))
                .scalars()
                .all()
            )
        assert subs == []
    finally:
        await _cleanup_account(account_id)
        await _cleanup_account(other_account_id)


async def test_trial_email_bound_to_another_account_is_409(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    email = f"shared-{uuid.uuid4().hex}@example.com"
    async with AsyncSessionLocal() as db:
        db.add(LiberclawUser(user_id=email, user_type="email", liberclaw_account_id=other_account_id))
        await db.commit()
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/trial",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "email": email, "days": 14},
        )
        assert resp.status_code == 409
        assert fake.sub_seq == 0

        async with AsyncSessionLocal() as db:
            subs = (
                (await db.execute(select(PlanSubscription).where(PlanSubscription.liberclaw_account_id == account_id)))
                .scalars()
                .all()
            )
        assert subs == []
    finally:
        await _cleanup_account(account_id)
        await _cleanup_account(other_account_id)


async def test_checkout_without_provider_subscription_id_is_502(async_client, monkeypatch):
    """A provider answer with no subscription id leaves a Revolut subscription nothing can
    locate again: 502 with the orphaned link logged, and no local row committed."""
    fake = _install_fake_provider(monkeypatch)
    errors: list[str] = []
    monkeypatch.setattr(liberclaw_routes.logger, "error", lambda msg, **kw: errors.append(msg))

    async def _no_subscription_id(**kwargs):
        return CheckoutResult(
            checkout_url="http://pay/orphan",
            provider_subscription_id=None,
            provider_customer_id="cust_orphan",
            order_id="setup_orphan",
        )

    monkeypatch.setattr(fake, "create_subscription", _no_subscription_id)

    account_id = uuid.uuid4()
    try:
        resp = await async_client.post(
            "/liberclaw/checkout",
            headers=HEADERS,
            json={
                "liberclaw_account_id": str(account_id),
                "email": "orphan@example.com",
                "tier": "starter",
                "redirect_url": "http://lc.example/callback",
            },
        )
        assert resp.status_code == 502
        assert any("http://pay/orphan" in m for m in errors)

        async with AsyncSessionLocal() as db:
            subs = (
                (await db.execute(select(PlanSubscription).where(PlanSubscription.liberclaw_account_id == account_id)))
                .scalars()
                .all()
            )
        assert subs == []
    finally:
        await _cleanup_account(account_id)


# --------------------------------------------------------------------- upgrade / EUR pinning


async def test_upgrade_happy_path_pins_eur(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    await _seed_lc_user(account_id)
    old_sub_id = await _seed_sub(account_id, tier="starter")
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/upgrade",
            headers=HEADERS,
            json={
                "liberclaw_account_id": str(account_id),
                "tier": "pro",
                "redirect_url": "http://lc.example/callback",
                "currency": "USD",  # smuggled — must be ignored
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        new_sub_id = uuid.UUID(body["subscription_id"])
        assert new_sub_id != old_sub_id

        assert fake.sub_currencies[-1] == "EUR"
        assert fake.sub_products[-1] == "liberclaw"

        new_sub = await _get_sub(new_sub_id)
        assert new_sub.tier == "pro"
        assert new_sub.status == "pending_upgrade"
    finally:
        await _cleanup_account(account_id)


async def test_upgrade_unknown_account_lets_manager_error_speak(async_client, monkeypatch):
    """No bridge row -> no resolvable email -> the manager's own ValueError surfaces as 400."""
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    resp = await async_client.post(
        "/liberclaw/subscription/upgrade",
        headers=HEADERS,
        json={"liberclaw_account_id": str(account_id), "tier": "pro", "redirect_url": "http://x"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------- cancel / resume / downgrade


async def test_cancel_happy_path(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    sub_id = await _seed_sub(account_id, tier="starter")
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/cancel", headers=HEADERS, json={"liberclaw_account_id": str(account_id)}
        )
        assert resp.status_code == 200
        assert "message" in resp.json()

        sub = await _get_sub(sub_id)
        assert sub.cancel_at_period_end is True
    finally:
        await _cleanup_account(account_id)


async def test_resume_happy_path(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    sub_id = await _seed_sub(account_id, tier="starter", cancel_at_period_end=True)
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/resume", headers=HEADERS, json={"liberclaw_account_id": str(account_id)}
        )
        assert resp.status_code == 200
        assert resp.json()["tier"] == "starter"

        sub = await _get_sub(sub_id)
        assert sub.cancel_at_period_end is False
    finally:
        await _cleanup_account(account_id)


async def test_downgrade_happy_path(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    sub_id = await _seed_sub(account_id, tier="pro")
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/downgrade",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "tier": "starter"},
        )
        assert resp.status_code == 200
        assert resp.json()["new_tier"] == "starter"

        sub = await _get_sub(sub_id)
        assert sub.pending_tier == "starter"
        assert fake.plan_change_products[-1] == "liberclaw"
    finally:
        await _cleanup_account(account_id)


# --------------------------------------------------------------------- trial / trial-eligibility


async def test_self_serve_trial_happy_path(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/trial",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "email": "trial@example.com", "days": 14},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "starter"  # SELF_SERVE_TRIAL_TIER[liberclaw]
        assert body["is_trial"] is True
        assert body["status"] == "active"

        async with AsyncSessionLocal() as db:
            lc_user = (
                await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
            ).scalar_one()
        assert lc_user.user_id == "trial@example.com"
    finally:
        await _cleanup_account(account_id)


async def test_self_serve_trial_days_out_of_bounds_is_422(async_client, monkeypatch):
    """days mirrors grant_trial's own 1-90 bound — start_self_serve_trial never checks it
    itself, so an out-of-range value must be rejected before it reaches the manager."""
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    try:
        for days in (3650, -5):
            resp = await async_client.post(
                "/liberclaw/subscription/trial",
                headers=HEADERS,
                json={"liberclaw_account_id": str(account_id), "email": "trial@example.com", "days": days},
            )
            assert resp.status_code == 422

        # Eligibility was never burned by the rejected attempts: a valid request still succeeds.
        ok = await async_client.post(
            "/liberclaw/subscription/trial",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "email": "trial@example.com", "days": 14},
        )
        assert ok.status_code == 200
    finally:
        await _cleanup_account(account_id)


async def test_trial_eligibility_unknown_account_is_no_email(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    resp = await async_client.get(
        "/liberclaw/subscription/trial-eligibility",
        headers=HEADERS,
        params={"liberclaw_account_id": str(account_id)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"eligible": False, "reason": "no_email"}


async def test_trial_eligibility_known_account_is_eligible(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    await _seed_lc_user(account_id)
    try:
        resp = await async_client.get(
            "/liberclaw/subscription/trial-eligibility",
            headers=HEADERS,
            params={"liberclaw_account_id": str(account_id)},
        )
        assert resp.status_code == 200
        assert resp.json() == {"eligible": True, "reason": None}
    finally:
        await _cleanup_account(account_id)


# --------------------------------------------------------------------- subscription-state


async def test_subscription_state_unknown_account_is_404_logged_info(async_client, monkeypatch):
    infos: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(liberclaw_routes.logger, "info", lambda msg: infos.append(msg))
    monkeypatch.setattr(liberclaw_routes.logger, "error", lambda msg, **kw: errors.append(msg))

    account_id = uuid.uuid4()
    resp = await async_client.get(
        "/liberclaw/subscription-state", headers=HEADERS, params={"liberclaw_account_id": str(account_id)}
    )
    assert resp.status_code == 404
    assert any(str(account_id) in m for m in infos)  # phase-0 convention: info, not error
    assert errors == []


async def test_subscription_state_includes_never_paid_pending_row(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    await _seed_sub(
        account_id,
        tier="starter",
        status="pending",
        current_period_start=None,
        current_period_end=None,
    )
    try:
        resp = await async_client.get(
            "/liberclaw/subscription-state", headers=HEADERS, params={"liberclaw_account_id": str(account_id)}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"
        assert resp.json()["tier"] == "starter"
    finally:
        await _cleanup_account(account_id)


# --------------------------------------------------------------------- admin: grant-trial / override-tier


async def test_admin_grant_trial_happy_path(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    await _seed_lc_user(account_id)
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/admin/grant-trial",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "tier": "pro", "days": 30, "granted_by": "ops@example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "pro"
        assert body["is_trial"] is True
        assert body["status"] == "active"
    finally:
        await _cleanup_account(account_id)


async def test_admin_override_tier_happy_path(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    await _seed_lc_user(account_id)
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/admin/override-tier",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "tier": "team"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "team"
        assert body["is_trial"] is False
        assert body["status"] == "active"

        async with AsyncSessionLocal() as db:
            lc_user = (
                await db.execute(select(LiberclawUser).where(LiberclawUser.liberclaw_account_id == account_id))
            ).scalar_one()
        assert lc_user.tier == "team"  # the snapshot's tier is the one actually enforced
    finally:
        await _cleanup_account(account_id)


async def test_admin_grant_trial_unbridged_account_is_404(async_client, monkeypatch):
    """No bridge row: the lc_users.tier write would be a logged no-op, so the snapshot would
    advertise a tier that enforces nothing. Refused instead, and no row is created."""
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/admin/grant-trial",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "tier": "pro", "days": 30, "granted_by": "ops@example.com"},
        )
        assert resp.status_code == 404

        async with AsyncSessionLocal() as db:
            subs = (
                (await db.execute(select(PlanSubscription).where(PlanSubscription.liberclaw_account_id == account_id)))
                .scalars()
                .all()
            )
        assert subs == []
    finally:
        await _cleanup_account(account_id)


async def test_admin_override_tier_unbridged_account_is_404(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/admin/override-tier",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "tier": "team"},
        )
        assert resp.status_code == 404

        async with AsyncSessionLocal() as db:
            subs = (
                (await db.execute(select(PlanSubscription).where(PlanSubscription.liberclaw_account_id == account_id)))
                .scalars()
                .all()
            )
        assert subs == []
    finally:
        await _cleanup_account(account_id)


# --------------------------------------------------------------------- admin: force-cancel (arm-1 ordering)


async def test_admin_force_cancel_happy_path(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    sub_id = await _seed_sub(account_id, tier="pro")
    sub_before = await _get_sub(sub_id)
    psub_id = sub_before.provider_subscription_id
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/admin/force-cancel",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id)},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        sub = await _get_sub(sub_id)
        assert sub.status == "cancelled"
        assert sub.provider_cancelled is True
        assert psub_id in fake.cancelled
    finally:
        await _cleanup_account(account_id)


async def test_admin_force_cancel_pre_marks_before_provider_call(async_client, monkeypatch):
    """The arm-1 contract: by the time the provider cancel is attempted, provider_cancelled
    is already True on the row being cancelled."""
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    await _seed_sub(account_id, tier="pro")

    call_log: list[bool] = []
    original = PaymentManager._cancel_on_provider

    async def spy(self, sub):
        call_log.append(sub.provider_cancelled)
        return await original(self, sub)

    monkeypatch.setattr(PaymentManager, "_cancel_on_provider", spy)
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/admin/force-cancel",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id)},
        )
        assert resp.status_code == 200
        assert call_log == [True]  # pre-marked before the (only) provider-cancel attempt
    finally:
        await _cleanup_account(account_id)


async def test_admin_force_cancel_provider_failure_rolls_back_pre_mark(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    sub_id = await _seed_sub(account_id, tier="pro")
    sub_before = await _get_sub(sub_id)
    fake.cancel_failures.add(sub_before.provider_subscription_id)
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/admin/force-cancel",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id)},
        )
        assert resp.status_code == 502

        sub = await _get_sub(sub_id)
        assert sub.status == "active"  # never went terminal
        assert sub.provider_cancelled is False  # pre-mark rolled back
    finally:
        await _cleanup_account(account_id)


async def test_admin_force_cancel_no_active_subscription_is_400(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    resp = await async_client.post(
        "/liberclaw/subscription/admin/force-cancel", headers=HEADERS, json={"liberclaw_account_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------- admin: extend


async def test_admin_extend_happy_path(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    base_end = datetime.now() + timedelta(days=10)
    sub_id = await _seed_sub(account_id, tier="pro", current_period_end=base_end)
    try:
        resp = await async_client.post(
            "/liberclaw/subscription/admin/extend",
            headers=HEADERS,
            json={"liberclaw_account_id": str(account_id), "days": 5},
        )
        assert resp.status_code == 200
        new_period_end = resp.json()["new_period_end"]
        assert "+00:00" in new_period_end or new_period_end.endswith("Z")  # offset-aware

        sub = await _get_sub(sub_id)
        expected_end = base_end + timedelta(days=5)
        assert abs((sub.current_period_end - expected_end).total_seconds()) < 1
    finally:
        await _cleanup_account(account_id)


# --------------------------------------------------------------------- flag-off: mutations refused, reads live


async def test_flag_off_refuses_every_mutating_subscription_route(async_client, monkeypatch):
    """Pre-flip, LiberClaw still owns billing and the webhook 200-skips liberclaw events: a
    checkout taken here would charge for a subscription nothing would activate."""
    fake = _install_fake_provider(monkeypatch)
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    account_id = uuid.uuid4()
    email = f"flagoff-{uuid.uuid4().hex}@example.com"
    requests = [
        ("/liberclaw/checkout", {"email": email, "tier": "starter", "redirect_url": "http://lc.example/cb"}),
        ("/liberclaw/subscription/upgrade", {"tier": "pro", "redirect_url": "http://lc.example/cb"}),
        ("/liberclaw/subscription/cancel", {}),
        ("/liberclaw/subscription/resume", {}),
        ("/liberclaw/subscription/downgrade", {"tier": "starter"}),
        ("/liberclaw/subscription/trial", {"email": email, "days": 14}),
        ("/liberclaw/subscription/admin/grant-trial", {"tier": "pro", "days": 30, "granted_by": "ops@example.com"}),
        ("/liberclaw/subscription/admin/override-tier", {"tier": "team"}),
        ("/liberclaw/subscription/admin/force-cancel", {}),
        ("/liberclaw/subscription/admin/extend", {"days": 5}),
    ]
    try:
        for path, body in requests:
            resp = await async_client.post(
                path, headers=HEADERS, json={"liberclaw_account_id": str(account_id), **body}
            )
            assert resp.status_code == 409, path
            assert resp.json()["detail"] == "LiberClaw billing is not enabled on this backend"

        assert fake.sub_seq == 0  # nothing reached the provider
        assert fake.cancelled == []

        state = await async_client.get(
            "/liberclaw/subscription-state", headers=HEADERS, params={"liberclaw_account_id": str(account_id)}
        )
        assert state.status_code == 404  # read path still live: no row for this account

        eligibility = await async_client.get(
            "/liberclaw/subscription/trial-eligibility",
            headers=HEADERS,
            params={"liberclaw_account_id": str(account_id)},
        )
        assert eligibility.status_code == 200

        invoices = await async_client.get(
            "/liberclaw/invoices", headers=HEADERS, params={"liberclaw_account_id": str(account_id)}
        )
        assert invoices.status_code == 200
    finally:
        await _cleanup_account(account_id)


async def test_flag_off_read_of_existing_subscription_state_is_200(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    await _seed_sub(account_id, tier="starter")
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    try:
        resp = await async_client.get(
            "/liberclaw/subscription-state", headers=HEADERS, params={"liberclaw_account_id": str(account_id)}
        )
        assert resp.status_code == 200
        assert resp.json()["tier"] == "starter"
    finally:
        await _cleanup_account(account_id)


# --------------------------------------------------------------------- webhook flag-off 200-skip


async def test_webhook_flag_off_skips_lclw_event(db, monkeypatch):
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", False)
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_flagoff",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:flagoff",
            provider_subscription_id="psub_flagoff",
            order_id="ord_flagoff",
        )
    )

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "pending"  # never activated: 200-skipped, not processed

    events = (
        (await db.execute(select(PlanSubscriptionEvent).where(PlanSubscriptionEvent.subscription_id == sub.id)))
        .scalars()
        .all()
    )
    assert events == []  # no event recorded: a later redelivery (flag on) still processes cleanly


async def test_webhook_flag_on_processes_lclw_event(db, monkeypatch):
    """Sanity counterpart: the same event activates the row once the flag is on."""
    monkeypatch.setattr(config, "LIBERCLAW_BILLING_ENABLED", True)
    account_id = uuid.uuid4()
    sub = PlanSubscription(
        user_id=None,
        tier="starter",
        provider="fake",
        status="pending",
        provider_subscription_id="psub_flagon",
        product=PRODUCT_LIBERCLAW,
        liberclaw_account_id=account_id,
    )
    db.add(sub)
    await db.flush()

    mgr = PaymentManager(FakeProvider(), db)
    await mgr.handle_event(
        PaymentEvent(
            provider="fake",
            type=PaymentEventType.order_completed,
            provider_event_id="ORDER_COMPLETED:flagon",
            provider_subscription_id="psub_flagon",
        )
    )

    refreshed = await db.get(PlanSubscription, sub.id)
    assert refreshed.status == "active"


# --------------------------------------------------------------------- foreign-order product check (retained HTTP path)


async def test_ltai_owned_sub_order_is_rejected_foreign(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    own_sub_id = f"psub_ltai_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(channel_data={"subscription_id": own_sub_id})
    async with AsyncSessionLocal() as db:
        user = User(email=f"lclw-b6-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.flush()
        db.add(
            PlanSubscription(
                user_id=user.id, tier="go", provider="revolut", provider_subscription_id=own_sub_id, status="active"
            )
        )
        await db.commit()
        await db.refresh(user)
    try:
        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "rejected_foreign"
    finally:
        await _cleanup(account_id=account_id, user_id=user.id)


async def test_lclw_owned_sub_order_is_not_rejected_foreign(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    own_sub_id = f"psub_lclw_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(channel_data={"subscription_id": own_sub_id})
    await _seed_sub(account_id, tier="starter", provider_subscription_id=own_sub_id)
    try:
        resp = await async_client.post(
            "/liberclaw/invoices",
            headers=HEADERS,
            json=_post(order_id=order_id, liberclaw_account_id=account_id, tier="starter"),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "issued"
    finally:
        await _cleanup_account(account_id)
        await _cleanup(account_id=account_id)


async def test_lclw_checkout_declined_event_is_not_rejected_foreign(async_client, monkeypatch):
    """checkout_declined/activation_refused/refunded events carry an order_id with no invoice
    ever issued against it — an LCLW hit here must not 409 LC's own order during the overlap
    window (the owned_event arm is narrowed the same way as the resolved-sub arm)."""
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(channel_data={})
    sub_id = await _seed_sub(account_id, tier="starter")
    async with AsyncSessionLocal() as db:
        db.add(
            PlanSubscriptionEvent(
                subscription_id=sub_id, event_type="checkout_declined", metadata_json={"order_id": order_id}
            )
        )
        await db.commit()
    try:
        resp = await async_client.post(
            "/liberclaw/invoices",
            headers=HEADERS,
            json=_post(order_id=order_id, liberclaw_account_id=account_id, tier="starter"),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "issued"
    finally:
        await _cleanup_account(account_id)
