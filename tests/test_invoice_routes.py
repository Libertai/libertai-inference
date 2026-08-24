"""Invoice route tests: list (own, newest first) + PDF download (owner-only, cached)."""

import time
import uuid
from datetime import datetime

from sqlalchemy import delete

from src.models.base import AsyncSessionLocal
from src.models.invoice import Invoice
from src.models.user import User
from src.routes.invoices.invoices import MAX_PAGE_SIZE
from src.services.auth_tokens import create_access_token
from src.services.invoice import issue_invoice


async def _auth_user() -> tuple[User, dict]:
    async with AsyncSessionLocal() as db:
        user = User(email=f"invoice-route-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}@example.com")
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


async def _issue(user: User, **overrides) -> Invoice:
    async with AsyncSessionLocal() as db:
        kwargs = {
            "user_id": user.id,
            "user_email": user.email,
            "external_reference": f"revolut:{uuid.uuid4().hex}",
            "gross_minor": 2000,
            "currency": "EUR",
            "tax_minor": 333,
            "payment_date": datetime(2026, 8, 1),
            "line_label": "Prepaid credits",
        }
        kwargs.update(overrides)
        invoice = await issue_invoice(db, **kwargs)
        await db.commit()
        await db.refresh(invoice)
    return invoice


async def _cleanup(*user_ids):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Invoice).where(Invoice.user_id.in_(user_ids)))
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.commit()


async def test_list_own_invoices_newest_first(async_client):
    user, headers = await _auth_user()
    try:
        first = await _issue(user, external_reference=f"revolut:{uuid.uuid4().hex}")
        second = await _issue(user, external_reference=f"revolut:{uuid.uuid4().hex}")

        resp = await async_client.get("/invoices", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        items = body["items"]
        assert [item["id"] for item in items] == [str(second.id), str(first.id)]
        assert items[0]["number"] == second.number
        assert items[0]["currency"] == "EUR"
        assert items[0]["net_amount"] == str(second.net_amount)  # Decimal -> exact string in JSON
    finally:
        await _cleanup(user.id)


async def test_page_zero_is_rejected(async_client):
    user, headers = await _auth_user()
    try:
        resp = await async_client.get("/invoices", headers=headers, params={"page": 0})
        assert resp.status_code == 422

        resp = await async_client.get("/invoices", headers=headers, params={"page_size": 0})
        assert resp.status_code == 422
    finally:
        await _cleanup(user.id)


async def test_page_size_over_cap_is_accepted_and_capped(async_client):
    assert MAX_PAGE_SIZE == 100  # the cap the route's min(page_size, MAX_PAGE_SIZE) enforces
    user, headers = await _auth_user()
    try:
        made = [await _issue(user, external_reference=f"revolut:{uuid.uuid4().hex}") for _ in range(3)]

        resp = await async_client.get("/invoices", headers=headers, params={"page_size": 500})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == len(made)
        assert body["total"] == len(made)
    finally:
        await _cleanup(user.id)


async def test_cannot_see_others_invoice_pdf(async_client):
    owner, owner_headers = await _auth_user()
    other, other_headers = await _auth_user()
    try:
        invoice = await _issue(owner)

        resp = await async_client.get(f"/invoices/{invoice.id}/pdf", headers=other_headers)
        assert resp.status_code == 404

        missing_resp = await async_client.get(f"/invoices/{uuid.uuid4()}/pdf", headers=owner_headers)
        assert missing_resp.status_code == 404
    finally:
        await _cleanup(owner.id, other.id)


async def test_pdf_download_content_type_and_cache_headers(async_client):
    user, headers = await _auth_user()
    try:
        invoice = await _issue(user)

        resp = await async_client.get(f"/invoices/{invoice.id}/pdf", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["content-disposition"] == f'attachment; filename="{invoice.number}.pdf"'
        assert resp.content.startswith(b"%PDF")
    finally:
        await _cleanup(user.id)


async def test_pdf_bytes_stable_across_downloads(async_client):
    user, headers = await _auth_user()
    try:
        invoice = await _issue(user)

        first = await async_client.get(f"/invoices/{invoice.id}/pdf", headers=headers)
        second = await async_client.get(f"/invoices/{invoice.id}/pdf", headers=headers)
        assert first.content == second.content
    finally:
        await _cleanup(user.id)
