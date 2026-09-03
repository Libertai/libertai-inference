"""LiberClaw invoice + billing-details endpoints: centralized issuance over the token-authed
/liberclaw channel. The Revolut merchant account is shared with inference's own LTAI product,
so most of this exercises the foreign-order screen (never invoice an order that isn't LiberClaw's).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select

from src.config import config
from src.interfaces.credits import CreditTransactionProvider
from src.models.base import AsyncSessionLocal
from src.models.credit_transaction import CreditTransaction
from src.models.invoice import Invoice
from src.models.liberclaw_billing_details import LiberclawBillingDetails
from src.models.plan_subscription import PlanSubscription
from src.models.plan_subscription_event import PlanSubscriptionEvent
from src.models.user import User
from src.services.invoice import issue_invoice
from src.services.payments.registry import payment_registry
from tests.test_payment_manager import FakeProvider

HEADERS = {"x-liberclaw-token": config.LIBERCLAW_SECRET}


class LiberclawFakeProvider(FakeProvider):
    """Adds the raw dict reads issue_for_liberclaw needs and tracks get_order call count for
    the lock-free-duplicate-precheck assertion. Reports as "revolut" so external_reference
    matches the design's literal "revolut:<order_id>" idempotency key.
    """

    def __init__(self):
        super().__init__()
        self.get_order_calls = 0
        self.cycles: dict[str, dict] = {}
        self.current_cycle_ids: dict[str, str] = {}

    def descriptor(self):
        d = super().descriptor()
        d.id = "revolut"
        return d

    async def get_order(self, order_id: str) -> dict:
        self.get_order_calls += 1
        return self.orders[order_id]

    async def get_cycle(self, provider_subscription_id: str, cycle_id: str) -> dict:
        return self.cycles[cycle_id]

    async def get_current_cycle(self, provider_subscription_id: str) -> dict:
        cycle_id = self.current_cycle_ids[provider_subscription_id]
        cycle = dict(self.cycles[cycle_id])
        cycle.setdefault("id", cycle_id)
        return cycle


def _install_fake_provider(monkeypatch) -> LiberclawFakeProvider:
    fake = LiberclawFakeProvider()
    monkeypatch.setitem(payment_registry._providers, "revolut", fake)
    return fake


def _order(**overrides) -> dict:
    order = {
        "state": "completed",
        "amount": 1200,
        "currency": "EUR",
        "type": "payment",
        "completed_at": "2026-09-01T10:00:00+00:00",
        "channel_data": {"subscription_id": "lc_sub_default", "subscription_cycle_id": "lc_cyc_default"},
    }
    order.update(overrides)
    return order


async def _make_user() -> User:
    async with AsyncSessionLocal() as db:
        user = User(email=f"lclw-inv-{uuid.uuid4().hex}@example.com")
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user


async def _invoice_count(account_id: uuid.UUID) -> int:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Invoice.id).where(Invoice.liberclaw_account_id == account_id))).scalars().all()
    return len(rows)


async def _cleanup(*, account_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None):
    async with AsyncSessionLocal() as db:
        if account_id is not None:
            await db.execute(delete(Invoice).where(Invoice.liberclaw_account_id == account_id))
            await db.execute(
                delete(LiberclawBillingDetails).where(LiberclawBillingDetails.liberclaw_account_id == account_id)
            )
        if user_id is not None:
            # Invoice.user_id is a RESTRICT FK (10-year retention): must go before the user.
            await db.execute(delete(Invoice).where(Invoice.user_id == user_id))
            await db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user_id))
            await db.execute(delete(PlanSubscription).where(PlanSubscription.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


def _post(order_id=None, provider_subscription_id=None, **overrides) -> dict:
    body = {
        "liberclaw_account_id": str(overrides.pop("liberclaw_account_id", uuid.uuid4())),
        "email": "buyer@example.com",
        "tier": "starter",
    }
    if order_id is not None:
        body["order_id"] = order_id
    if provider_subscription_id is not None:
        body["provider_subscription_id"] = provider_subscription_id
    body.update(overrides)
    return body


# --------------------------------------------------------------------- 1. issue by order_id


async def test_issue_by_order_id_happy_path(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(amount=1200, currency="EUR")
    try:
        async with AsyncSessionLocal() as db:
            db.add(LiberclawBillingDetails(liberclaw_account_id=account_id, name="Acme SAS", country="France"))
            await db.commit()

        resp = await async_client.post(
            "/liberclaw/invoices",
            headers=HEADERS,
            json=_post(order_id=order_id, liberclaw_account_id=account_id, tier="starter"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "issued"
        assert body["number"].startswith("LCLW-")

        async with AsyncSessionLocal() as db:
            invoice = (
                await db.execute(select(Invoice).where(Invoice.id == uuid.UUID(body["invoice_id"])))
            ).scalar_one()
        assert invoice.line_label == "LiberClaw Starter subscription"  # derived from tier, not caller text
        assert invoice.gross_amount == Decimal("12.00")
        assert invoice.vat_amount == Decimal("2.00")  # EUR, no line_items -> back-calculated 20%
        assert invoice.buyer["email"] == "buyer@example.com"
        assert invoice.buyer["name"] == "Acme SAS"  # from liberclaw_billing_details, not the request
        assert invoice.provider_subscription_id == "lc_sub_default"
        assert invoice.cycle_id == "lc_cyc_default"  # taken from the order's channel_data
    finally:
        await _cleanup(account_id=account_id)


# --------------------------------------------------------------------- 2. duplicate re-submit


async def test_duplicate_resubmit_same_number_no_provider_call(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order()
    body = _post(order_id=order_id, liberclaw_account_id=account_id, tier="pro")
    try:
        first = await async_client.post("/liberclaw/invoices", headers=HEADERS, json=body)
        assert first.json()["status"] == "issued"
        calls_after_first = fake.get_order_calls

        second = await async_client.post("/liberclaw/invoices", headers=HEADERS, json=body)
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert second.json()["number"] == first.json()["number"]
        assert fake.get_order_calls == calls_after_first  # lock-free pre-check: no provider I/O
    finally:
        await _cleanup(account_id=account_id)


# --------------------------------------------------------------------- 3. issue by provider_subscription_id


async def test_issue_by_provider_subscription_id(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    sub_id = "psub_lc_1"
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.cycles["cyc_1"] = {"order_id": order_id, "start_date": "2026-09-01", "end_date": "2026-10-01"}
    fake.current_cycle_ids[sub_id] = "cyc_1"
    fake.orders[order_id] = _order(channel_data={"subscription_id": sub_id})
    try:
        resp = await async_client.post(
            "/liberclaw/invoices",
            headers=HEADERS,
            json=_post(provider_subscription_id=sub_id, liberclaw_account_id=account_id, tier="team"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "issued"

        async with AsyncSessionLocal() as db:
            invoice = (
                await db.execute(select(Invoice).where(Invoice.id == uuid.UUID(body["invoice_id"])))
            ).scalar_one()
        assert invoice.external_reference == f"revolut:{order_id}"
        assert invoice.provider_subscription_id == sub_id
    finally:
        await _cleanup(account_id=account_id)


# --------------------------------------------------------------------- 4. foreign-order rejections


async def test_reject_order_with_topup_ext_ref(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(merchant_order_ext_ref=f"topup:{uuid.uuid4()}", channel_data={})
    try:
        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "rejected_foreign"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id)


async def test_reject_order_matching_own_plan_subscription(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    own_sub_id = f"psub_own_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(channel_data={"subscription_id": own_sub_id})
    user = await _make_user()
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                PlanSubscription(
                    user_id=user.id,
                    tier="go",
                    provider="revolut",
                    provider_subscription_id=own_sub_id,
                    status="active",
                )
            )
            await db.commit()

        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "rejected_foreign"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id, user_id=user.id)


async def test_reject_order_matching_credit_transaction(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(channel_data={})
    user = await _make_user()
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                CreditTransaction(
                    user_id=user.id,
                    amount=10,
                    amount_left=10,
                    provider=CreditTransactionProvider.revolut,
                    external_reference=f"revolut:{order_id}",
                )
            )
            await db.commit()

        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "rejected_foreign"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id, user_id=user.id)


async def test_reject_order_matching_multiple_subscription_events(async_client, monkeypatch):
    """A retried charge can log more than one event carrying the same order id: the ownership
    probe must not 500 (MultipleResultsFound) on more than one match."""
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(channel_data={})
    user = await _make_user()
    try:
        async with AsyncSessionLocal() as db:
            sub = PlanSubscription(user_id=user.id, tier="go", provider="revolut", status="active")
            db.add(sub)
            await db.flush()
            db.add(
                PlanSubscriptionEvent(
                    subscription_id=sub.id, event_type="activated", metadata_json={"order_id": order_id}
                )
            )
            db.add(
                PlanSubscriptionEvent(
                    subscription_id=sub.id, event_type="renewed", metadata_json={"order_id": order_id}
                )
            )
            await db.commit()

        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "rejected_foreign"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id, user_id=user.id)


async def test_reject_order_matching_multiple_plan_subscriptions(async_client, monkeypatch):
    """Same MultipleResultsFound hazard, on the plan_subscriptions probe."""
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    own_sub_id = f"psub_own_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(channel_data={"subscription_id": own_sub_id})
    user_a = await _make_user()
    user_b = await _make_user()
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                PlanSubscription(
                    user_id=user_a.id,
                    tier="go",
                    provider="revolut",
                    provider_subscription_id=own_sub_id,
                    status="cancelled",
                )
            )
            db.add(
                PlanSubscription(
                    user_id=user_b.id,
                    tier="go",
                    provider="revolut",
                    provider_subscription_id=own_sub_id,
                    status="cancelled",
                )
            )
            await db.commit()

        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "rejected_foreign"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id, user_id=user_a.id)
        await _cleanup(user_id=user_b.id)


# --------------------------------------------------------------------- 5. NULL-guard


async def test_null_channel_data_not_rejected_by_null_plan_subscription(async_client, monkeypatch):
    """The false-positive trap: a NULL channel_data.subscription_id must never match a
    plan_subscriptions row whose provider_subscription_id also happens to be NULL."""
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(channel_data={})
    user = await _make_user()
    try:
        async with AsyncSessionLocal() as db:
            db.add(PlanSubscription(user_id=user.id, tier="go", provider="revolut", status="pending"))
            await db.commit()

        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "issued"
    finally:
        await _cleanup(account_id=account_id, user_id=user.id)


# --------------------------------------------------------------------- 6. refund / zero


async def test_refund_order_skipped(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = {"state": "completed", "type": "refund", "channel_data": {}}
    try:
        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped_refund"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id)


async def test_zero_amount_order_skipped(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(amount=0)
    try:
        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped_zero"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id)


# --------------------------------------------------------------------- 6b. settlement gate


async def test_unsettled_order_is_unresolvable_not_issued(async_client, monkeypatch):
    """A cycle names its order at creation, before it settles — the sweep's provider_subscription_id
    path must not mint an invoice for a charge that hasn't (or hasn't yet) gone through."""
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order(state="pending")
    try:
        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 422
        assert resp.json()["status"] == "unresolvable"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id)


async def test_malformed_order_payload_is_unresolvable_with_envelope(async_client, monkeypatch):
    """order_invoice_fields' ValueError (missing amount/currency) must stay inside the
    {status: ...} envelope, not leak out as FastAPI's bare {detail: ...} shape."""
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = {"state": "completed", "type": "payment", "channel_data": {}}  # no amount/currency
    try:
        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 422
        assert resp.json()["status"] == "unresolvable"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id)


# --------------------------------------------------------------------- 7. positive assertion mismatch


async def test_subscription_id_mismatch_rejected(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    sub_id = "psub_claimed"
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.cycles["cyc_1"] = {"order_id": order_id}
    fake.current_cycle_ids[sub_id] = "cyc_1"
    fake.orders[order_id] = _order(channel_data={"subscription_id": "psub_actual_other"})
    try:
        resp = await async_client.post(
            "/liberclaw/invoices",
            headers=HEADERS,
            json=_post(provider_subscription_id=sub_id, liberclaw_account_id=account_id),
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "rejected_foreign"
        assert await _invoice_count(account_id) == 0
    finally:
        await _cleanup(account_id=account_id)


# --------------------------------------------------------------------- 8. list + pdf ownership


async def test_list_and_pdf_ownership(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    other_account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order()
    try:
        issue_resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        invoice_id = issue_resp.json()["invoice_id"]

        own_list = await async_client.get(
            "/liberclaw/invoices", headers=HEADERS, params={"liberclaw_account_id": str(account_id)}
        )
        assert own_list.status_code == 200
        assert own_list.json()["total"] == 1
        assert own_list.json()["items"][0]["id"] == invoice_id

        foreign_list = await async_client.get(
            "/liberclaw/invoices", headers=HEADERS, params={"liberclaw_account_id": str(other_account_id)}
        )
        assert foreign_list.json()["total"] == 0

        own_pdf = await async_client.get(
            f"/liberclaw/invoices/{invoice_id}/pdf", headers=HEADERS, params={"liberclaw_account_id": str(account_id)}
        )
        assert own_pdf.status_code == 200
        assert own_pdf.headers["cache-control"] == "no-store"

        foreign_pdf = await async_client.get(
            f"/liberclaw/invoices/{invoice_id}/pdf",
            headers=HEADERS,
            params={"liberclaw_account_id": str(other_account_id)},
        )
        assert foreign_pdf.status_code == 404
    finally:
        await _cleanup(account_id=account_id)


# --------------------------------------------------------------------- 9. billing-details roundtrip


async def test_billing_details_roundtrip(async_client):
    account_id = uuid.uuid4()
    params = {"liberclaw_account_id": str(account_id)}
    try:
        empty = await async_client.get("/liberclaw/billing-details", headers=HEADERS, params=params)
        assert empty.status_code == 200
        assert empty.json()["name"] is None

        put_resp = await async_client.put(
            "/liberclaw/billing-details",
            headers=HEADERS,
            params=params,
            json={"name": "Acme SAS", "country": "France"},
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["name"] == "Acme SAS"
        assert put_resp.json()["country"] == "France"

        get_resp = await async_client.get("/liberclaw/billing-details", headers=HEADERS, params=params)
        assert get_resp.json()["name"] == "Acme SAS"

        del_resp = await async_client.delete("/liberclaw/billing-details", headers=HEADERS, params=params)
        assert del_resp.status_code == 200

        final = await async_client.get("/liberclaw/billing-details", headers=HEADERS, params=params)
        assert final.json()["name"] is None
    finally:
        await _cleanup(account_id=account_id)


# --------------------------------------------------------------------- 10. auth


async def test_wrong_token_rejected(async_client):
    resp = await async_client.get(
        "/liberclaw/billing-details",
        headers={"x-liberclaw-token": "wrong"},
        params={"liberclaw_account_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 401


async def test_previous_secret_accepted_during_rotation(async_client, monkeypatch):
    monkeypatch.setattr(config, "LIBERCLAW_SECRET_PREVIOUS", "old-secret-during-rotation")
    resp = await async_client.get(
        "/liberclaw/billing-details",
        headers={"x-liberclaw-token": "old-secret-during-rotation"},
        params={"liberclaw_account_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 200


async def test_non_ascii_token_rejected_not_500(async_client):
    """hmac.compare_digest raises TypeError on a non-ASCII str; headers decode as latin-1, so a
    forged token containing a byte >= 0x80 must still 401, never 500. httpx's str headers are
    ASCII-only, so the raw byte is sent as bytes (it accepts bytes header values unencoded)."""
    resp = await async_client.get(
        "/liberclaw/billing-details",
        headers={"x-liberclaw-token": b"\x80invalid"},
        params={"liberclaw_account_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------- 6c. cross-series disclosure


async def test_ltai_order_number_never_crosses_channel(async_client, monkeypatch):
    """A pre-existing LTAI invoice for this order id must never surface (or leak its number)
    over the LiberClaw channel — the pre-check falls through to the ownership screen instead,
    which (re-)identifies the order as inference's own and 409s it."""
    fake = _install_fake_provider(monkeypatch)
    account_id = uuid.uuid4()
    order_id = f"ord_{uuid.uuid4().hex}"
    user = await _make_user()
    try:
        async with AsyncSessionLocal() as db:
            await issue_invoice(
                db,
                user_id=user.id,
                user_email=user.email,
                external_reference=f"revolut:{order_id}",
                gross_minor=1000,
                currency="USD",
                tax_minor=None,
                payment_date=datetime(2026, 8, 1),
                line_label="LibertAI usage credits",
            )
            await db.commit()
        # Make the order identify as inference's own topup so the fallthrough ownership check catches it.
        fake.orders[order_id] = _order(merchant_order_ext_ref=f"topup:{user.id}", channel_data={})

        resp = await async_client.post(
            "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, liberclaw_account_id=account_id)
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["status"] == "rejected_foreign"
        assert body.get("number") is None
    finally:
        await _cleanup(account_id=account_id, user_id=user.id)


# --------------------------------------------------------------------- request-shape validation


async def test_unknown_tier_is_422(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    order_id = f"ord_{uuid.uuid4().hex}"
    fake.orders[order_id] = _order()
    resp = await async_client.post(
        "/liberclaw/invoices", headers=HEADERS, json=_post(order_id=order_id, tier="enterprise")
    )
    assert resp.status_code == 422


async def test_both_order_references_given_is_422(async_client):
    resp = await async_client.post(
        "/liberclaw/invoices",
        headers=HEADERS,
        json=_post(order_id="ord_x", provider_subscription_id="sub_y"),
    )
    assert resp.status_code == 422


async def test_neither_order_reference_given_is_422(async_client):
    resp = await async_client.post("/liberclaw/invoices", headers=HEADERS, json=_post())
    assert resp.status_code == 422


# --------------------------------------------------------------------- 11. subscription-cycles listing


async def test_subscription_cycles_walks_chain_newest_first(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    sub_id = "psub_chain"
    fake.cycles["cyc_3"] = {
        "order_id": "ord_3",
        "start_date": "2026-09-01",
        "end_date": "2026-10-01",
        "previous_cycle_id": "cyc_2",
    }
    fake.cycles["cyc_2"] = {
        "order_id": "ord_2",
        "start_date": "2026-08-01",
        "end_date": "2026-09-01",
        "previous_cycle_id": "cyc_1",
    }
    fake.cycles["cyc_1"] = {"order_id": "ord_1", "start_date": "2026-07-01", "end_date": "2026-08-01"}
    fake.current_cycle_ids[sub_id] = "cyc_3"

    resp = await async_client.get(
        "/liberclaw/subscription-cycles", headers=HEADERS, params={"provider_subscription_id": sub_id}
    )
    assert resp.status_code == 200
    cycles = resp.json()["cycles"]
    assert [c["cycle_id"] for c in cycles] == ["cyc_3", "cyc_2", "cyc_1"]
    assert [c["order_id"] for c in cycles] == ["ord_3", "ord_2", "ord_1"]
    assert cycles[0]["start_date"] == "2026-09-01"


async def test_subscription_cycles_includes_null_order_id(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    sub_id = "psub_null_order"
    fake.cycles["cyc_1"] = {"order_id": None, "start_date": "2026-07-01", "end_date": "2026-08-01"}
    fake.current_cycle_ids[sub_id] = "cyc_1"

    resp = await async_client.get(
        "/liberclaw/subscription-cycles", headers=HEADERS, params={"provider_subscription_id": sub_id}
    )
    assert resp.status_code == 200
    cycles = resp.json()["cycles"]
    assert len(cycles) == 1
    assert cycles[0]["order_id"] is None


async def test_subscription_cycles_caps_at_36(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)
    sub_id = "psub_long_chain"
    n = 40
    for i in range(n):
        cycle_id = f"cyc_{i}"
        previous = f"cyc_{i + 1}" if i + 1 < n else None
        fake.cycles[cycle_id] = {"order_id": f"ord_{i}", "previous_cycle_id": previous}
    fake.current_cycle_ids[sub_id] = "cyc_0"

    resp = await async_client.get(
        "/liberclaw/subscription-cycles", headers=HEADERS, params={"provider_subscription_id": sub_id}
    )
    assert resp.status_code == 200
    cycles = resp.json()["cycles"]
    assert len(cycles) == 36
    assert cycles[0]["cycle_id"] == "cyc_0"
    assert cycles[-1]["cycle_id"] == "cyc_35"


async def test_subscription_cycles_unresolvable_sub_is_422(async_client, monkeypatch):
    fake = _install_fake_provider(monkeypatch)

    async def _raise(*args, **kwargs):
        raise ValueError("Subscription unknown_sub has no current cycle")

    fake.get_current_cycle = _raise

    resp = await async_client.get(
        "/liberclaw/subscription-cycles", headers=HEADERS, params={"provider_subscription_id": "unknown_sub"}
    )
    assert resp.status_code == 422


async def test_subscription_cycles_requires_token(async_client, monkeypatch):
    _install_fake_provider(monkeypatch)
    resp = await async_client.get(
        "/liberclaw/subscription-cycles",
        headers={"x-liberclaw-token": "wrong"},
        params={"provider_subscription_id": "psub_x"},
    )
    assert resp.status_code == 401
