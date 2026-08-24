"""Billing-details + invoice list/PDF endpoints."""

import uuid

from fastapi import Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select

from src.interfaces.invoices import (
    BillingDetailsResponse,
    BillingDetailsUpdate,
    InvoiceListResponse,
    InvoiceResponse,
)
from src.models.base import AsyncSessionLocal
from src.models.invoice import Invoice
from src.models.user import User
from src.models.user_billing_details import UserBillingDetails
from src.routes.invoices import router
from src.services.auth import get_current_user
from src.services.invoice_pdf import get_or_render_pdf

MAX_PAGE_SIZE = 100


@router.get("/billing-details")
async def get_billing_details(user: User = Depends(get_current_user)) -> BillingDetailsResponse:
    async with AsyncSessionLocal() as db:
        details = await db.get(UserBillingDetails, user.id)
        if details is None:
            return BillingDetailsResponse()
        return BillingDetailsResponse(**details.as_snapshot())


@router.put(
    "/billing-details", description="Full replace: omitted fields are cleared, so always send the complete object"
)
async def update_billing_details(
    body: BillingDetailsUpdate, user: User = Depends(get_current_user)
) -> BillingDetailsResponse:
    async with AsyncSessionLocal() as db:
        details = await db.get(UserBillingDetails, user.id)
        if details is None:
            details = UserBillingDetails(user_id=user.id)
            db.add(details)
        for field, value in body.model_dump().items():
            setattr(details, field, value)
        await db.commit()
        await db.refresh(details)
        return BillingDetailsResponse(**details.as_snapshot())


@router.get("")
async def list_invoices(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1),
    user: User = Depends(get_current_user),
) -> InvoiceListResponse:
    page_size = min(page_size, MAX_PAGE_SIZE)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Invoice)
                .where(Invoice.user_id == user.id)
                .order_by(Invoice.issued_at.desc(), Invoice.seq.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
        total = (
            await db.execute(select(func.count()).select_from(Invoice).where(Invoice.user_id == user.id))
        ).scalar_one()
        return InvoiceListResponse(
            items=[InvoiceResponse.model_validate(row, from_attributes=True) for row in rows], total=total
        )


@router.get("/{invoice_id}/pdf")
async def download_invoice_pdf(invoice_id: uuid.UUID, user: User = Depends(get_current_user)) -> Response:
    async with AsyncSessionLocal() as db:
        invoice = (
            await db.execute(select(Invoice).where(Invoice.id == invoice_id, Invoice.user_id == user.id))
        ).scalar_one_or_none()
        if invoice is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
        pdf_bytes = await get_or_render_pdf(db, invoice)
        await db.commit()
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{invoice.number}.pdf"',
                "Cache-Control": "no-store",
            },
        )
