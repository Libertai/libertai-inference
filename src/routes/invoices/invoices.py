"""Billing-details endpoints (Task 7 adds the invoice list/PDF endpoints to this router)."""

from fastapi import Depends

from src.interfaces.invoices import BillingDetailsResponse, BillingDetailsUpdate
from src.models.base import AsyncSessionLocal
from src.models.user import User
from src.models.user_billing_details import UserBillingDetails
from src.routes.invoices import router
from src.services.auth import get_current_user


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
